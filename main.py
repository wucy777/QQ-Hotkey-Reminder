#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QQ Hotkey Reminder —— QQ 快捷提醒助手

从名单里导入待提醒的人，按一个全局快捷键，程序自动把「下一位待提醒人」的
QQ 号输入到 QQ 的搜索框里（同时把提醒话术复制到剪贴板），人工确认后发送。
省去每次手动输入 QQ 号搜索的重复劳动。

用法、名单格式、配置说明见程序内「说明」页或 README.md。
打包单 exe：见 build.bat（PyInstaller --onefile --icon app.ico --collect-all customtkinter）
"""

import json
import os
import queue as _queue
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox

try:
    import ctypes
except ImportError:                      # 非 Windows 兜底
    ctypes = None

try:
    import customtkinter as ctk
except ImportError:
    ctk = None

try:
    import keyboard
    import pyperclip
except ImportError as _e:
    keyboard = None
    pyperclip = None
    _IMPORT_ERROR = _e
else:
    _IMPORT_ERROR = None

APP_NAME = "QQ Hotkey Reminder"
APP_TITLE = "QQ 快捷提醒助手"
__version__ = "1.1.0"

CONFIG_NAME = "config.json"
LOG_NAME = "error.log"
APP_DIR_NAME = "QQHotkeyReminder"          # 数据目录：%APPDATA%\QQHotkeyReminder
ELEVATED_FLAG = "--elevated"               # 已提权重启的标记（防止反复重启）

ST_PENDING = "待处理"
ST_TYPING = "输入中"
ST_DONE = "已输入"
ST_SKIPPED = "已跳过"
ST_FAILED = "发送失败"

BADGE_COLORS = {
    ST_PENDING: "#5b6472",
    ST_TYPING: "#f59e0b",
    ST_DONE: "#16a34a",
    ST_SKIPPED: "#6b7280",
    ST_FAILED: "#dc2626",
}

# ============ 默认配置（config.json 缺失或损坏时用它重新生成） ============
DEFAULT_CONFIG = {
    "hotkeys": {
        "next": "f8",        # 输入当前这位的QQ号，并切到下一位
        "repeat": "f7",      # 重新输入上一位
        "skip": "f9",        # 跳过当前这位，并自动输入下一位
        "failed": "f10",     # 标记刚刚那一位「发送失败」并顶置到列表最前
        "quit": "ctrl+f12"   # 结束监听并汇总
    },
    "type_delay": 0.03,               # 模拟打字时每个字符的间隔（秒）
    "clear_before_type": True,        # 输入前先「全选+删除」清空搜索框
    "search_by_name_fallback": True,  # qq_map 里没找到时，改为直接输入姓名搜索
    "auto_copy_message": True,        # 每次输入后把提醒话术复制到剪贴板
    "line_mode": False,               # 提醒页「按行识别」勾选框的状态（实时写入）
    "message_template": "{name}你好，麻烦尽快完成一下哦，看到消息回我一下，谢谢！",
    # 空表：默认配置只是模板，不能内置任何示例名字，否则会混进真实运行时的查询结果
    "qq_map": {}
}
# =========================================================================


# ---------------- 基础工具 ----------------

def get_app_dir():
    """exe（或脚本）所在目录，作为默认配置的基准位置。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def resource_path(rel):
    """打包后从解包目录取内置资源，源码运行时从脚本目录取。"""
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", get_app_dir())
    else:
        base = get_app_dir()
    return os.path.join(base, rel)


def read_text(path):
    with open(path, "r", encoding="utf-8-sig") as f:
        return f.read()


def write_text(path, text):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


# 提醒页顶部固定提示（不可编辑）
LIST_HINT = (
    "把待提醒的名单整段粘到下面就行，不用整理格式：\n"
    "· 默认：用配置里的名字去整段文字里检索，找到就标记\n"
    "· 勾选「按行识别」：一行 = 一个人，每行整体去配置里找\n"
    "· 独占一行的「名字 QQ号」优先于配置里的映射（名字不在配置里就新增）\n"
    "· 名字后跟中文逗号或英文逗号加 QQ 号，就像「张三，10001」\n"
    "· 输入的文字里别带手机号：11 位数字会被误当成 QQ 号拿去搜索\n"
    "· 识别后没匹配到的内容会列在下方让你复核（按行识别时带原行号）\n"
    "· 右边名单可以点 × 删除（删错了重新识别即可）"
)


def try_parse_json(text):
    """能解析成字典就返回字典，否则返回 None。"""
    if text is None:
        return None
    try:
        data = json.loads(text)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def default_config_text():
    return json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2) + "\n"


def merged_config(data):
    """磁盘配置 + 默认配置的合并结果，缺的字段用默认值补齐（只影响运行，不改文件）。"""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if isinstance(data, dict):
        for k, v in data.items():
            if k in ("hotkeys", "qq_map") and isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg


# ---------------- 配置编辑框的展示格式（中文标签、qq_map 一行一人） ----------------

SCALAR_KEYS = ("type_delay", "clear_before_type", "search_by_name_fallback",
               "auto_copy_message", "line_mode", "message_template")
BOOL_KEYS = ("clear_before_type", "search_by_name_fallback", "auto_copy_message",
             "line_mode")
HOTKEY_KEYS = ("next", "repeat", "skip", "failed", "quit")
KNOWN_TOP = {"hotkeys", "qq_map", *SCALAR_KEYS}

# 界面上显示的中文标签（保存进 JSON 时仍用 hotkeys.next 这类键名）
HOTKEY_LABELS = {
    "next": "输入下一位被提醒人qq",
    "repeat": "重输当前被提醒人qq",
    "skip": "跳过当前这位",
    "failed": "标记当前这位发送失败并置顶",
    "quit": "结束并汇总",
}
SCALAR_LABELS = {
    "type_delay": "模拟打字每个字符的间隔（秒）",
    "clear_before_type": "输入前是否先清空搜索框",
    "search_by_name_fallback": "没找到qq号时是否按姓名搜索",
    "auto_copy_message": "是否自动复制提醒话术",
    "line_mode": "提醒页是否默认勾选「按行识别」",
    "message_template": "提醒话术模板（{name} 替换成对方名字）",
}
LABEL_TO_KEY = {}
for _k, _v in HOTKEY_LABELS.items():
    LABEL_TO_KEY[_v] = ("hotkeys", _k)
for _k, _v in SCALAR_LABELS.items():
    LABEL_TO_KEY[_v] = ("scalar", _k)


def _norm_dict(d):
    d = {k: v for k, v in d.items() if not (isinstance(v, dict) and not v)}
    return json.dumps(d, sort_keys=True, ensure_ascii=False)


def _displayable(data):
    """把任意 JSON 配置收敛成编辑框能完整表达的形式。

    返回 (可展示配置, 被忽略的字段列表)：未知的顶层字段 / 未知快捷键会被丢弃，
    缺失的已知字段用默认值补齐——这样配置页始终能看到并修改每一个可改项
    （旧版本配置文件里没有的新快捷键也能显示出来）。
    """
    dropped, out = [], {}
    for k, v in data.items():
        if k == "hotkeys" and isinstance(v, dict):
            keep = {a: b for a, b in v.items() if a in HOTKEY_KEYS}
            dropped += [f"hotkeys.{a}" for a in v if a not in HOTKEY_KEYS]
            out[k] = keep
        elif k in KNOWN_TOP:
            out[k] = v
        else:
            dropped.append(k)
    return merged_config(out), dropped


def config_to_lines(cfg):
    """JSON 配置 -> 编辑框文本（中文标签；qq_map 一行一人：名字 qq）。"""
    out = ["# 快捷键（写法参考 keyboard 库，如 f8、ctrl+f12、alt+q）"]
    hk = cfg.get("hotkeys") or {}
    for k in HOTKEY_KEYS:
        if hk.get(k):
            out.append(f"{HOTKEY_LABELS[k]} = {hk[k]}")
    out.append("")
    for k in SCALAR_KEYS:
        if k in cfg and cfg[k] is not None:
            v = cfg[k]
            if k in BOOL_KEYS:
                v = "true" if v else "false"
            out.append(f"{SCALAR_LABELS[k]} = {v}")
    out.append("")
    out.append("# 名字和 QQ 号，一行一个人（空格或逗号分隔）")
    for k, v in (cfg.get("qq_map") or {}).items():
        out.append(f"{k} {v}")
    return "\n".join(out) + "\n"


def parse_config_lines(text):
    """编辑框文本 -> (配置 dict, None)；格式有误时 (None, 错误信息)。"""
    cfg = {"hotkeys": {}, "qq_map": {}}
    for no, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("＃"):
            continue
        if "=" not in line:
            # 没有等号：当作 qq_map 的一行「名字 qq」。
            # 但配置项/中文标签漏写「=」时（如「type_delay 0.03」）要明确报错，
            # 不能静默变成一个叫 type_delay 的「人」。
            if (line in SCALAR_KEYS or line in LABEL_TO_KEY
                    or line.startswith("hotkeys.") or line.startswith("qq_map.")):
                return None, f"第 {no} 行：应以「=」分隔，如「{line} = 值」"
            for lbl in LABEL_TO_KEY:
                if line.startswith(lbl):
                    return None, f"第 {no} 行：应以「=」分隔，如「{lbl} = 值」"
            parts = re.split(r"[\s,，]+", line)
            parts = [p for p in parts if p]
            if len(parts) < 2:
                return None, f"第 {no} 行：应写成「名字 QQ号」或「标签 = 值」：{line}"
            qq = parts[-1]
            if not QQ_RE.match(qq):
                return None, f"第 {no} 行：QQ 号应是 5~11 位数字：{line}"
            name = " ".join(parts[:-1]).strip()
            if not name:
                return None, f"第 {no} 行：缺少名字"
            cfg["qq_map"][name] = qq
            continue
        k, _sep, v = line.partition("=")
        k = k.strip()
        v = v.strip()
        # 1) 中文标签（界面展示用的写法）
        kind_key = LABEL_TO_KEY.get(k)
        if kind_key:
            kind, key = kind_key
            if kind == "hotkeys":
                if not v:
                    return None, f"第 {no} 行：{k} 的值不能为空"
                cfg["hotkeys"][key] = v
                continue
            if key == "type_delay":
                try:
                    cfg[key] = float(v)
                except ValueError:
                    return None, f"第 {no} 行：{k} 应为数字（秒）"
            elif key in BOOL_KEYS:
                cfg[key] = _parse_bool(v, no, k)
                if isinstance(cfg[key], str):
                    return None, cfg[key]
            else:
                cfg[key] = v
            continue
        # 2) 兼容直接写原始键名（hotkeys.next / type_delay / qq_map.名字）
        if k.startswith("hotkeys."):
            sub = k[len("hotkeys."):].strip()
            if sub not in HOTKEY_KEYS:
                return None, f"第 {no} 行：未知快捷键项「hotkeys.{sub}」"
            if not v:
                return None, f"第 {no} 行：快捷键 {sub} 的值不能为空"
            cfg["hotkeys"][sub] = v
        elif k.startswith("qq_map."):
            name = k[len("qq_map."):].strip()
            if not name:
                return None, f"第 {no} 行：qq_map 缺少名字"
            cfg["qq_map"][name] = v
        elif k in SCALAR_KEYS:
            if k == "type_delay":
                try:
                    cfg[k] = float(v)
                except ValueError:
                    return None, f"第 {no} 行：type_delay 应为数字（秒）"
            elif k in BOOL_KEYS:
                cfg[k] = _parse_bool(v, no, k)
                if isinstance(cfg[k], str):
                    return None, cfg[k]
            else:
                cfg[k] = v
        else:
            return None, f"第 {no} 行：不认识的配置项「{k}」"
    return cfg, None


def _parse_bool(v, no, label):
    """返回 True/False，或错误信息字符串。"""
    lv = v.lower()
    if lv in ("true", "1", "yes", "是", "开"):
        return True
    if lv in ("false", "0", "no", "否", "关"):
        return False
    return f"第 {no} 行：{label} 应为 true 或 false"


# ---------------- 配置位置（固定 %APPDATA%\QQHotkeyReminder\config.json） ----------------

def get_data_dir():
    """程序数据目录：%APPDATA%\\QQHotkeyReminder。"""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, APP_DIR_NAME)


def get_default_config_path():
    """配置文件的固定位置。"""
    return os.path.join(get_data_dir(), CONFIG_NAME)


def ensure_config(target):
    """确保 target 处有一份可用的配置文件。

    返回 (配置文本, 提示消息列表)。
    - 文件不存在              -> 生成默认配置
    - 文件存在但不是有效 JSON  -> 备份损坏文件后重新生成默认配置
    - 目标位置完全不可写       -> 返回 None，由调用方决定回退位置
    """
    msgs = []
    if os.path.isfile(target):
        text = None
        try:
            text = read_text(target)
        except Exception as e:
            msgs.append(f"读取配置失败：{e}")
        if text is not None and try_parse_json(text) is not None:
            return text, msgs
        if text is not None:
            bak = f"{target}.bad.{time.strftime('%Y%m%d-%H%M%S')}"
            try:
                os.replace(target, bak)
                msgs.append(f"配置文件格式错误，已把损坏文件备份为：{os.path.basename(bak)}")
            except Exception:
                msgs.append("配置文件格式错误，且备份失败（将直接覆盖）")
    try:
        text = default_config_text()
        write_text(target, text)
        msgs.append(f"已生成默认配置文件：{target}")
        return text, msgs
    except Exception as e:
        msgs.append(f"无法在 {target} 生成配置：{e}")
        return None, msgs


def startup_config():
    """程序启动时调用：确保数据目录里的配置可用。

    返回 (配置文件路径, 配置文本, 消息列表)
    """
    msgs = []
    target = get_default_config_path()
    text, m = ensure_config(target)
    msgs += m
    if text is None:
        # 数据目录不可写（极罕见），回退到程序目录，保证程序能启动
        fallback = os.path.join(get_app_dir(), CONFIG_NAME)
        msgs.append(f"数据目录不可写，已改用程序目录：{fallback}")
        text, m = ensure_config(fallback)
        msgs += m
        if text is not None:
            target = fallback
    return target, text, msgs


# ---------------- 名单解析 ----------------

FULLWIDTH = str.maketrans("０１２３４５６７８９", "0123456789")
CJK = re.compile(r"[\u4e00-\u9fff]")
NOTE = re.compile(r"[（(【\[『「].*?[)）\]』」]")   # 括号里的备注，如 张三（出差）
QQ_RE = re.compile(r"^\d{5,11}$")
# 表头/标题行特征（如「未完成名单（共 12 人）」「序号 姓名 学号」）
HEADER_RE = re.compile(
    r"(名单|序号|姓名|学号|昵称|合计|总计|人数|共\s*\d|\d+\s*人|"
    r"以下|如下|统计|清单|列表|汇总)")
# 「名字, QQ号」这种直接写明的写法（逗号类分隔符后的 5-11 位数字）
EXPLICIT_QQ_RE = re.compile(r"[ \t]*[,，;；:：/|][ \t]*(\d{5,11})(?!\d)")
# 紧跟在名字后面的括号备注，如「王五（已请假）」「张三(未交)」
NOTE_AT_RE = re.compile(r"[ \t]*[（(【\[『「][^)）\]』」]{0,20}[)）\]』」]")
# 文本里成对出现的「名字, QQ号」，用于补充 qq_map 里没有的人
NAME_QQ_RE = re.compile(r"([\u4e00-\u9fff·]{2,8})[ \t]*[,，;；:：/|][ \t]*(\d{5,11})(?!\d)")
CJK_NAME_RE = re.compile(r"[\u4e00-\u9fff]{2,}")
# 独占一行的「名字 QQ号」map 格式条目：同名时优先于配置里的映射。
# 行内出现的「名字 学号」（如 王五 20231101）不算，只有整行就是名字+QQ号才算。
# 用 [ \t] 而不是 \s，避免把「名字」和下一行的数字粘成一条。
MAP_LINE_RE = re.compile(
    r"^[ \t]*(?:[（(]?\d{1,3}[ \t]*[.、,，)）][ \t]*)?"
    r"([\u4e00-\u9fff·]{2,8})[ \t,，;；:：/|]+(\d{5,11})[ \t]*$", re.M)
# 像日期（20231101 = 2023-11-01）的 8 位数字，按学号处理，不当 QQ 号。
# 「名字 QQ号」用空格分隔时靠它排除学号；写成「名字, 20231101」带逗号则强制当 QQ 号。
STUDENT_ID_RE = re.compile(r"^20\d{2}(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])$")
# 逗号类分隔符（出现这些说明是用户显式指定 QQ 号，不做学号排除）
QQ_SEP_CHARS = ",，;；:：/|"
# 一行开头的序号，如「1. 」「3、」「(2) 」
LINE_INDEX_RE = re.compile(r"^[ \t]*(?:[（(]?\d{1,3}[ \t]*[.、,，)）][ \t]*)+")


def scan_lines(text, qq_map):
    """按行识别：每一行整体当作一个名字去找（勾选「按行识别」时用）。

    返回 (命中列表, 未匹配行列表, 未出现的名单成员列表)。

    规则：
      · 一行 = 一个人。先剥掉行首序号、括号备注，再拿整行去 qq_map 里找。
      · 独占一行的「名字 QQ号」优先于 qq_map（数字不能是学号样式）。
      · 整行在 qq_map 里找不到 -> 记进「未匹配行」，按原行号展示，方便找 OCR 错字。
    """
    hits, unmatched, found = [], [], set()
    lines = text.splitlines()

    # 1) 先把「独占一行的 名字 QQ号」扫出来
    for i, raw in enumerate(lines, 1):
        line = re.sub(r"[#＃].*$", "", raw).strip().strip("\u3000")
        if not line:
            continue
        stripped = LINE_INDEX_RE.sub("", line)
        m = MAP_LINE_RE.match(line) or MAP_LINE_RE.match(stripped)
        if not m:
            continue
        name, qq = m.group(1), m.group(2)
        base = line if MAP_LINE_RE.match(line) else stripped
        sep = base[base.find(name) + len(name):base.find(qq)]
        if not any(c in sep for c in QQ_SEP_CHARS) and STUDENT_ID_RE.match(qq):
            continue                      # 空格分隔的日期样式数字 = 学号，不是 QQ 号
        if name in found:
            continue
        found.add(name)
        hits.append({"name": name, "qq": qq, "count": 1,
                     "extra": name not in qq_map, "_line": i})

    # 2) 其余每行整体当作名字去匹配
    used = {h["_line"] for h in hits}
    for i, raw in enumerate(lines, 1):
        if i in used:
            continue
        line = re.sub(r"[#＃].*$", "", raw).strip().strip("\u3000")
        if not line:
            continue
        line = LINE_INDEX_RE.sub("", line).strip()
        if not line:
            continue
        # 候选顺序：去掉括号备注的整行 -> 整行 -> 去掉尾部数字后的部分
        # 最后一项是为了「名字 学号」这种写法仍能认出人（学号不当 QQ 号，走 map）；
        # 只剥数字，不剥别的汉字，避免「张三 李四」这种一行两人被悄悄当成一个人
        cands = [NOTE.sub("", line).strip(), line]
        without_tail_num = re.sub(r"[ \t,，;；:：/|]+\d{5,11}[ \t]*$", "", line).strip()
        cands.append(NOTE.sub("", without_tail_num).strip())
        cands.append(without_tail_num)
        name = next((c for c in cands if c and c in qq_map), None)
        if name is None:
            unmatched.append({"line": i, "text": raw.strip()})
            continue
        if name in found:
            continue
        found.add(name)
        hits.append({"name": name, "qq": str(qq_map[name]), "count": 1,
                     "extra": False, "_line": i})

    missed = [n for n in qq_map if str(n).strip() and str(n).strip() not in found]
    return hits, unmatched, missed


def _clean_leftover(text):
    """整理剩余文本：已标记的位置用空格顶替，避免相邻残字被拼成新的假名字。"""
    keep = []
    for raw in text.splitlines():
        line = re.sub(r"\s+", " ", raw.replace("\x00", " ")).strip()
        if not line:
            continue
        if HEADER_RE.search(line):
            # 标题/表头类文字（未完成名单、共 X 人、序号…），不是人名，丢掉
            continue
        if not CJK_NAME_RE.search(line):
            continue
        keep.append(line)
    return "\n".join(keep).strip()


def scan_text(text, qq_map):
    """在整段文本里按 qq_map 的名字检索（OCR 错字场景用）。

    返回 (命中列表, 剩余文本, 完全没找到的名字列表)。

    QQ 号取值优先级（高 -> 低）：
      1. 文本里独占一行的「名字 QQ号」/「名字, QQ号」条目（用户显式指定）
      2. 名字后面紧跟的「, QQ号」
      3. 配置（qq_map）里的映射
    名字在配置里没有、但以 map 格式写进文本的，会作为额外的人一起提醒。
    命中列表顺序 = 配置里的名单顺序，额外的人排在其后（按出现位置）。
    文本本身不会被修改，命中的字符位置用 covered 记录，用来算「剩余文本」。
    """
    covered = [False] * len(text)

    def cover(a, b):
        for k in range(a, min(b, len(text))):
            covered[k] = True

    def free(a, b):
        return not any(covered[a:b])

    # 0) 先扫出「独占一行」的 map 格式条目：名字 -> 显式 QQ 号
    map_line_qq, extras = {}, []
    for m in MAP_LINE_RE.finditer(text):
        name, qq = m.group(1), m.group(2)
        # 「名字 20231101」这种空格分隔的日期样式按学号处理，不当 QQ 号
        sep = text[m.start(1) + len(name):m.start(2)]
        if not any(c in sep for c in QQ_SEP_CHARS) and STUDENT_ID_RE.match(qq):
            continue
        if name in map_line_qq:
            continue
        map_line_qq[name] = qq
        extras.append((m.start(1), name, qq))

    hits, found = [], set()

    # 1) 按名字长度从长到短检索，避免短名字先吃掉长名字的字符
    #    （如名单里同时有「张三」和「张三三」时，短的先匹配会让长的漏掉）
    names = [str(n).strip() for n in qq_map]
    names = [n for n in names if n]
    for name in sorted(set(names) | set(map_line_qq), key=len, reverse=True):
        in_cfg = name in qq_map
        start, count, explicit = 0, 0, None
        positions = []                       # 命中区间，给左侧高亮用（与识别一致）
        while True:
            i = text.find(name, start)
            if i < 0:
                break
            j = i + len(name)
            if not free(i, j):
                start = i + 1
                continue
            cover(i, j)
            positions.append((i, j))
            count += 1
            # 名字后面紧跟的括号备注（如「王五（已请假）」）属于这个人，一起吃掉，
            # 免得它被当成「可能漏人」的可疑文字吓人一跳
            m = NOTE_AT_RE.match(text, j)
            if m:
                cover(j, m.end())
            m = EXPLICIT_QQ_RE.match(text, j)
            if m:
                explicit = m.group(1)
                cover(j, m.end())
            start = j
        if not count:
            # 配置里没有、文本里也没出现该名字 -> 不算这个人
            continue
        found.add(name)
        # 优先级：行内显式 QQ > 独占一行的 map 条目 > 配置映射
        qq = explicit or map_line_qq.get(name) or (str(qq_map[name]) if in_cfg else None)
        hits.append({"name": name, "qq": qq, "count": count,
                     "extra": not in_cfg, "pos": positions})

    # 命中顺序恢复成配置里的名单顺序，文本里新增的人排在其后（按出现位置）
    order = {str(n).strip(): i for i, n in enumerate(qq_map)}
    pos_of = {n: p for p, n, _ in extras}
    hits.sort(key=lambda h: (order.get(h["name"], len(order)),
                             pos_of.get(h["name"], 0)))

    # 2) 文本里另有「名字, QQ号」写在句子中间、且该名字还没被识别的，也补进来
    tail = []
    for m in NAME_QQ_RE.finditer(text):
        name, qq = m.group(1), m.group(2)
        if name in found or not free(m.start(1), m.end(2)):
            continue
        if STUDENT_ID_RE.match(qq):
            continue
        cover(m.start(1), m.end(2))
        tail.append((m.start(1), {"name": name, "qq": qq, "count": 1, "extra": True,
                                  "pos": [(m.start(1), m.start(1) + len(name))]}))
        found.add(name)
    tail.sort(key=lambda x: x[0])
    hits += [h for _, h in tail]

    leftover = "".join("\x00" if c else ch for ch, c in zip(text, covered))
    missed = [n for n in qq_map if str(n).strip() and str(n).strip() not in found]
    return hits, _clean_leftover(leftover), missed


# ---------------- 图标（PIL 程序化绘制，白色线条，无需素材文件） ----------------

def _draw_icon(name, size=20):
    from PIL import Image, ImageDraw
    s = size * 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    col = (255, 255, 255, 255)
    w = max(2, s // 11)

    def L(x1, y1, x2, y2):
        d.line([x1 * s, y1 * s, x2 * s, y2 * s], fill=col, width=w)

    def RR(x1, y1, x2, y2, r=0.06, fill=None):
        d.rounded_rectangle([x1 * s, y1 * s, x2 * s, y2 * s], radius=r * s,
                            outline=col, width=w, fill=fill)

    def PG(pts):
        d.line([(x * s, y * s) for x, y in pts] + [(pts[0][0] * s, pts[0][1] * s)],
               fill=col, width=w, joint="curve")

    def F(pts):
        d.polygon([(x * s, y * s) for x, y in pts], fill=col)

    if name == "file":
        RR(0.22, 0.08, 0.78, 0.92, 0.07)
        L(0.34, 0.34, 0.66, 0.34)
        L(0.34, 0.50, 0.66, 0.50)
        L(0.34, 0.66, 0.58, 0.66)
    elif name == "play":
        F([(0.32, 0.16), (0.84, 0.50), (0.32, 0.84)])
    elif name == "stop":
        RR(0.24, 0.24, 0.76, 0.76, 0.10, fill=col)
    elif name == "skip":
        F([(0.12, 0.20), (0.45, 0.50), (0.12, 0.80)])
        F([(0.48, 0.20), (0.80, 0.50), (0.48, 0.80)])
        L(0.88, 0.18, 0.88, 0.82)
    elif name == "reset":
        d.arc([0.15 * s, 0.15 * s, 0.85 * s, 0.85 * s], start=25, end=305, fill=col, width=w)
        F([(0.96, 0.40), (0.70, 0.50), (0.88, 0.72)])
    elif name == "trash":
        L(0.16, 0.24, 0.84, 0.24)
        RR(0.40, 0.10, 0.60, 0.24, 0.04)
        RR(0.24, 0.24, 0.76, 0.90, 0.08)
        L(0.42, 0.42, 0.42, 0.72)
        L(0.58, 0.42, 0.58, 0.72)
    elif name == "folder":
        PG([(0.10, 0.80), (0.10, 0.24), (0.38, 0.24), (0.47, 0.36), (0.90, 0.36), (0.90, 0.80)])
    elif name == "save":
        PG([(0.16, 0.12), (0.66, 0.12), (0.84, 0.30), (0.84, 0.88), (0.16, 0.88)])
        RR(0.34, 0.12, 0.60, 0.34, 0.02)
        RR(0.32, 0.52, 0.68, 0.88, 0.03)
    elif name == "download":
        L(0.50, 0.10, 0.50, 0.56)
        F([(0.32, 0.48), (0.68, 0.48), (0.50, 0.72)])
        L(0.18, 0.64, 0.18, 0.86)
        L(0.18, 0.86, 0.82, 0.86)
        L(0.82, 0.64, 0.82, 0.86)
    elif name == "house":
        PG([(0.50, 0.10), (0.92, 0.46), (0.78, 0.46), (0.78, 0.88), (0.22, 0.88),
            (0.22, 0.46), (0.08, 0.46)])
    elif name == "close":
        L(0.22, 0.22, 0.78, 0.78)
        L(0.78, 0.22, 0.22, 0.78)
    elif name == "pin":
        d.ellipse([0.24 * s, 0.10 * s, 0.76 * s, 0.62 * s], outline=col, width=w)
        d.ellipse([0.42 * s, 0.28 * s, 0.58 * s, 0.44 * s], fill=col)
        F([(0.34, 0.55), (0.66, 0.55), (0.50, 0.94)])
    return img.resize((size, size), Image.LANCZOS)


# ---------------- 后台输入线程 ----------------

class RemindWorker(threading.Thread):
    """在独立线程里执行模拟按键，避免卡住快捷键钩子和界面。"""

    def __init__(self, ui_queue):
        super().__init__(daemon=True)
        self.tasks = _queue.Queue()
        self.ui_queue = ui_queue

    def submit_type(self, payload):
        self.tasks.put(("type", payload))

    def run(self):
        while True:
            item = self.tasks.get()
            if item is None:
                break
            _, p = item
            try:
                self._do_type(p)
            except Exception as e:
                self.ui_queue.put(("type_error", {"uid": p.get("uid"), "msg": str(e)}))

    def _do_type(self, p):
        cfg = p["cfg"]
        text = p["qq"] or p["name"]
        if cfg.get("clear_before_type", True):
            keyboard.send("ctrl+a")
            time.sleep(0.1)
            keyboard.send("delete")
            time.sleep(0.1)
        keyboard.write(text, delay=float(cfg.get("type_delay", 0.03)))
        if cfg.get("auto_copy_message", True):
            try:
                tpl = cfg.get("message_template", "")
                try:
                    msg = tpl.format(name=p["name"])
                except Exception:
                    msg = tpl
                pyperclip.copy(msg)
            except Exception as e:
                self.ui_queue.put(("clip_error", str(e)))
        self.ui_queue.put(("typed", {"uid": p.get("uid"), "name": p["name"],
                                      "qq": p["qq"]}))


# ---------------- 界面 ----------------

HELP_TEXT = """\
一、干什么用的

把待提醒的名单整段粘进「提醒」页，程序按配置里的名字自动换成 QQ 号。
之后点一下 QQ 左上角的搜索框，按快捷键：
    自动清空搜索框 → 输入下一位的 QQ 号 → 提醒话术复制到剪贴板
你只要：点搜索结果里的人 → 聊天框 Ctrl+V → 发送 → 回搜索框按下一位。

二、怎么用（三步）

    1. 把名单整段粘到左边，不用整理格式，点「识别名单」
       （想让每行单独算一个人，就勾上旁边的「按行识别」）
    2. 点「开始监听快捷键」
    3. 点一下 QQ 的搜索框，按 F8 依次处理；结束后按 Ctrl+F12 看汇总

三、快捷键（「配置」页可改）

    F8         输入下一位被提醒人 qq
    F7         重输当前被提醒人 qq
    F9         跳过当前这位
    F10        标记当前这位「发送失败」并置顶（再按一次取消标记）
    Ctrl+F12   结束并汇总

    什么时候用 F10：有些好友不允许临时会话，从群里发起聊天会发送失败，
    或者 QQ 抽风发不出去。这时按一下 F10，这个人会被标红并排到名单最前面，
    方便你稍后单独换方式联系，不影响其他人和 F8 的进度。

四、识别规则

    两种方式，用「待提醒名单」旁边的「按行识别」勾选框切换（勾选状态会记住）：

    【默认：整段检索】拿配置里的名字，去你粘的整段文字里找，找到就标记：
      · 名字出现几次就标记几次，找到的人都进右边队列
      · 名字被别的字夹在中间也能找到（如 OCR 出来的一长串文字）
      · 识别完如果还有没被标记的文字，会显示在下方让你复核
        （用 OCR 认错字时最容易漏人，看一眼这段文字就能发现）

    【按行识别】一行 = 一个人，每行整体拿去配置里找：
      · 适合名单本来就是一行一个名字、或者 OCR 结果一行一个人
      · 没匹配到的行会按原行号单独列出（第 N 行：内容），方便一行行核对错字
      · 行里带序号、括号备注都能认（如「1. 张三」「王五（已请假）」）
      · 注意：「张三 李四」写在同一行只会匹配不到，拆成两行即可

    两种方式都支持：
      · 独占一行的「名字 QQ号」优先于配置里的映射；名字不在配置里就当作新增
      · 名字后面跟「，QQ号」或「, QQ号」时，用你写的 QQ 号（中文逗号也行）
      · 用空格分隔的日期样式数字（如 20231101）按学号处理，不当 QQ 号

五、注意事项

    · 按快捷键时鼠标焦点必须在 QQ 的搜索框里：程序会先「全选+删除」清空那个
      输入框再打字，焦点在聊天输入框时按会清掉你打的字。
    · 待提醒文字里不要包含手机号：11 位数字会被当成 QQ 号拿去搜索，
      程序不会自动区分手机号和 QQ 号。
    · 程序启动时会自动申请管理员权限（模拟按键需要）；取消授权就退出了。
    · 个别安全软件会拦截模拟按键，误报请自行加白。
    · 按 QQ 号搜索能精确找到好友；按姓名搜索时注意别点错人。
    · 名单里认出的人可以随时点右边的 × 删掉，删完不影响其他人的处理顺序。

六、配置文件

    存在 %APPDATA%\\QQHotkeyReminder\\config.json（exe 旁边不放东西）。
    「配置」页可以直接改，改完点「保存配置」；格式是「名字 QQ号」一行一人。
    「按行识别」勾选框的状态会实时存进配置（那一项叫 line_mode）。
    不想用了，点「彻底删除本软件数据」即可删掉整个文件夹并退出程序。
"""


class App(ctk.CTk):
    CFG_BUTTONS = ("btn_read", "btn_save", "btn_delete_data")

    # 窗口尺寸：按「中窗口」直觉启动——开屏大小跟屏幕可用区域按比例走，
    # 别人的屏幕比你的大/小，窗口就等比大一点/小一点；再封顶/托底，
    # 保证任何屏幕都放得下且不会「开屏即全屏」。单位表见 _screen_fit 注释。
    SIZE_FRAC_W = 2 / 3     # 可用宽度的 2/3：你的 1707 逻辑屏上 ≈ 1120
    SIZE_FRAC_H = 5 / 7     # 可用高度的 5/7：你的 1067 逻辑屏上 ≈ 700
    CAP_W, CAP_H = 1400, 860        # 大屏封顶（避免开屏即全屏）
    FLOOR_W, FLOOR_H = 900, 600     # 极小屏托底（再小的屏以屏幕实际可用为准）

    def __init__(self):
        super().__init__()
        self.title(f"{APP_TITLE} v{__version__}")
        w, h, mw, mh = self._screen_fit()
        self.geometry(f"{w}x{h}")
        self.minsize(mw, mh)
        self._min_win = (mw, mh)
        try:
            ico = resource_path("app.ico")
            if os.path.isfile(ico):
                self.iconbitmap(ico)
        except Exception:
            pass

        # 字号整体调大一档：13/11 在 1080p 上偏小，看着累
        self.font = ctk.CTkFont(family="Microsoft YaHei UI", size=15)
        self.font_small = ctk.CTkFont(family="Microsoft YaHei UI", size=13)
        self.font_bold = ctk.CTkFont(family="Microsoft YaHei UI", size=15, weight="bold")
        self.font_title = ctk.CTkFont(family="Microsoft YaHei UI", size=19, weight="bold")
        self.font_mono = ctk.CTkFont(family="Consolas", size=14)
        self.font_row = ctk.CTkFont(family="Microsoft YaHei UI", size=16)    # 队列行主文字
        self.font_badge = ctk.CTkFont(family="Microsoft YaHei UI", size=14)  # 队列行次要文字/徽章
        self._icons = {}

        self.listening = False
        self._hk_handles = []
        self.queue = []            # [{"uid","name","qq","status","count","extra"}]
        self.idx = 0               # 处理指针：下一位待输入的人在 queue 里的位置
        self._uid_seq = 0
        self._del_hit = {}         # uid -> 删除按钮的命中区域（Canvas 自绘用）
        self.current_config_path = None
        self.cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        self.ui_queue = _queue.Queue()
        self.worker = RemindWorker(self.ui_queue) if keyboard is not None else None
        if self.worker:
            self.worker.start()

        self._build_header()
        self._build_tabs()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(80, self._poll)
        self._startup_load()

    # ---------- 图标 / 控件工厂 ----------

    def _next_uid(self):
        self._uid_seq += 1
        return self._uid_seq

    def icon(self, name, size=18):
        key = (name, size)
        if key not in self._icons:
            try:
                pil = _draw_icon(name, 20)
                self._icons[key] = ctk.CTkImage(light_image=pil, dark_image=pil,
                                                size=(size, size))
            except Exception:
                self._icons[key] = None
        return self._icons[key]

    def _btn(self, master, text, icon_name, command, kind="subtle", width=118,
             small=False):
        palette = {
            "primary": (("#3b82f6", "#3b82f6"), ("#2563eb", "#2563eb"), None),
            "green": (("#22c55e", "#22c55e"), ("#16a34a", "#16a34a"), None),
            "red": (("#ef4444", "#ef4444"), ("#dc2626", "#dc2626"), None),
            "subtle": (("gray86", "#333a46"), ("gray78", "#414a5a"), ("gray15", "#e8eaed")),
            # 危险操作：红字红边、不抢眼
            "danger": (("gray90", "#1f232b"), ("#3a2027", "#3a2027"),
                       ("#b91c1c", "#f87171")),
        }[kind]
        kw = dict(text=text, command=command, width=width,
                  height=26 if small else 34, corner_radius=8 if small else 9,
                  font=self.font_small if small else self.font,
                  fg_color=palette[0], hover_color=palette[1])
        if palette[2]:
            kw["text_color"] = palette[2]
            if kind == "danger":
                kw["border_width"] = 1
                kw["border_color"] = ("#e5a3a3", "#7f3038")
        img = self.icon(icon_name, 15 if small else 18)
        if img:
            kw.update(image=img, compound="left")
        return ctk.CTkButton(master, **kw)

    def _logbox(self, master, height=92):
        box = ctk.CTkTextbox(master, height=height, font=self.font_small, wrap="word",
                             fg_color=("gray94", "#161920"), text_color=("gray35", "gray65"))
        box.configure(state="disabled")
        return box

    @staticmethod
    def _log(box, msg):
        box.configure(state="normal")
        box.insert("end", time.strftime("[%H:%M:%S] ") + msg + "\n")
        box.see("end")
        box.configure(state="disabled")

    # ---------- 顶部标题栏 ----------

    def _screen_fit(self):
        """启动尺寸 = 屏幕可用区域 × 固定比例（SIZE_FRAC_*），再封顶/托底。

        屏幕大一号窗口就大一号、小一号就等比缩小；大屏封顶 1400x860
        （避免开屏即全屏），极小屏托底 900x600（再小以屏幕实际可用为准）。

        本机（Win11 + 150% 缩放 + 2560x1600 屏）实测过的单位表，别再猜：
          winfo_screenwidth/height -> 1707x1067   逻辑（= 物理 2560x1600 / 1.5）
          geometry()/minsize()     -> 吃逻辑值，CTk 内部乘缩放
          winfo_width/height       -> 物理像素（映射后 1400 逻辑实测 2100 物理）
        所以全程用逻辑像素运算；千万不要拿 winfo_width（物理）来比，
        否则会把窗口越"缩"越大。
        """
        try:
            sw = self.winfo_screenwidth()
            sh = self.winfo_screenheight()
        except Exception:
            return self.FLOOR_W, self.FLOOR_H, self.FLOOR_W, self.FLOOR_H
        # 任务栏/标题栏留白：宽度留 24，高度留 90（标题栏 + 任务栏 + 边距）
        avail_w = max(640.0, sw - 24)
        avail_h = max(480.0, sh - 90)
        w = int(min(self.CAP_W, max(self.FLOOR_W, avail_w * self.SIZE_FRAC_W)))
        h = int(min(self.CAP_H, max(self.FLOOR_H, avail_h * self.SIZE_FRAC_H)))
        # 极小屏上托底值也可能超过可用区域：以屏幕为准，保证一定放得下
        w = min(w, int(avail_w))
        h = min(h, int(avail_h))
        # 启动尺寸 = 最小尺寸：窗口打开就是中窗口，要大自己拖/最大化
        return w, h, w, h

    def _build_header(self):
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=18, pady=(14, 2))
        png = resource_path("app_icon.png")
        if os.path.isfile(png):
            try:
                from PIL import Image as PILImage
                img = PILImage.open(png)
                self._title_icon = ctk.CTkImage(light_image=img, dark_image=img, size=(30, 30))
                ctk.CTkLabel(head, image=self._title_icon, text="").pack(side="left", padx=(0, 10))
            except Exception:
                pass
        ctk.CTkLabel(head, text=APP_TITLE, font=self.font_title).pack(side="left")
        ctk.CTkLabel(head, text=f"v{__version__}", font=self.font_small,
                     text_color=("gray45", "gray60")).pack(side="left", padx=(8, 0), pady=(4, 0))
        mode = ctk.CTkSegmentedButton(head, values=["深色", "浅色"], height=28,
                                      font=self.font_small, command=self._switch_mode)
        mode.set("深色")
        mode.pack(side="right")

    @staticmethod
    def _switch_mode(v):
        ctk.set_appearance_mode("dark" if v == "深色" else "light")

    # ---------- 页签 ----------

    def _build_tabs(self):
        self.tabs = ctk.CTkTabview(self, anchor="nw")
        self.tabs.pack(fill="both", expand=True, padx=14, pady=(6, 14))
        try:
            self.tabs._segmented_button.configure(font=self.font, height=34)
        except Exception:
            pass
        tab_run = self.tabs.add("提醒")
        tab_cfg = self.tabs.add("配置")
        tab_help = self.tabs.add("说明")
        self._build_run_tab(tab_run)
        self._build_cfg_tab(tab_cfg)
        self._build_help_tab(tab_help)

    # ---------- 提醒页 ----------

    def _build_run_tab(self, f):
        # 提醒页整体网格：
        #   row 0 = 标题行（表头直接放进左右两列，不再用跨列容器，保证和下面面板严格对齐）
        #   row 1 = 内容行
        f.grid_columnconfigure(0, weight=1, minsize=340)   # 左列
        f.grid_columnconfigure(1, weight=2, minsize=560)   # 右列
        f.grid_rowconfigure(1, weight=1)

        head_l = ctk.CTkFrame(f, fg_color="transparent")
        head_l.grid(row=0, column=0, sticky="ew", padx=(4, 12), pady=(2, 6))
        ctk.CTkLabel(head_l, text="待提醒名单", font=self.font_bold).pack(side="left")
        self.line_mode_var = ctk.BooleanVar(
            value=bool(self.cfg.get("line_mode", False)))
        self.line_mode_cb = ctk.CTkCheckBox(
            head_l, text="按行识别", variable=self.line_mode_var,
            command=self.op_toggle_line_mode, font=self.font,
            checkbox_width=20, checkbox_height=20, corner_radius=5)
        self.line_mode_cb.pack(side="left", padx=(14, 0))

        head_r = ctk.CTkFrame(f, fg_color="transparent")
        head_r.grid(row=0, column=1, sticky="ew", pady=(2, 6))
        self._title_lbl = ctk.CTkLabel(head_r, text="名单队列", font=self.font_bold)
        self._title_lbl.pack(side="left")
        self.count_lbl = ctk.CTkLabel(head_r, text="共 0 人", font=self.font,
                                      text_color=("gray50", "gray60"))
        self.count_lbl.pack(side="left", padx=10)
        self.line_mode_hint = ctk.CTkLabel(
            head_r, text="", font=self.font, text_color=("gray50", "gray60"))
        self.line_mode_hint.pack(side="left", padx=6)

        # 左列：名单输入
        left = ctk.CTkFrame(f, fg_color="transparent")
        left.grid(row=1, column=0, sticky="nsew", padx=(4, 12))
        left.grid_columnconfigure(0, weight=1)
        left.grid_rowconfigure(1, weight=1)
        # 固定提示文本：不可编辑。wraplength 随容器宽度自适应，
        # 否则字号或窗口一变就会换行错乱（出现单字孤行）
        hint = ctk.CTkLabel(
            left, text=LIST_HINT, justify="left", anchor="w", font=self.font,
            text_color=("gray40", "gray65"), fg_color=("gray90", "#1a1e26"),
            corner_radius=8)
        hint.grid(row=0, column=0, sticky="ew", pady=(0, 6), ipadx=10, ipady=8)
        self.hint_lbl = hint

        def _fit_hint(e=None):
            try:
                w = left.winfo_width() - 24        # 减去 ipadx(10*2) 与余量
                if w > 120 and abs(hint.cget("wraplength") - w) > 8:
                    hint.configure(wraplength=w)
            except Exception:
                pass
        left.bind("<Configure>", _fit_hint)
        self.list_text = ctk.CTkTextbox(left, font=self.font, wrap="word",
                                        width=280, fg_color=("gray94", "#161920"))
        self.list_text.grid(row=1, column=0, sticky="nsew", pady=(0, 8))
        bar_in = ctk.CTkFrame(left, fg_color="transparent")
        bar_in.grid(row=2, column=0, sticky="ew")
        # 窄屏时按钮可压缩：不给它们把整列顶宽的机会
        self._btn(bar_in, "从文件导入", "file", self.op_import_file, width=104
                  ).pack(side="left")
        self._btn(bar_in, "识别名单", "download", self.op_parse, kind="primary",
                  width=104).pack(side="left", padx=6)
        self._btn(bar_in, "清空", "trash", self.op_clear_list, width=70).pack(side="left")

        # 右列：队列 + 控制
        right = ctk.CTkFrame(f, fg_color="transparent")
        right.grid(row=1, column=1, sticky="nsew")
        right.grid_columnconfigure(0, weight=1)
        # 队列区（row 0）吸收多余空间；其余行按内容高度
        right.grid_rowconfigure(0, weight=1)

        self.rows_frame = self._build_rows_area(right)
        self.rows_frame.grid(row=0, column=0, sticky="nsew")

        prog = ctk.CTkFrame(right, fg_color="transparent")
        prog.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        prog.grid_columnconfigure(0, weight=1)
        self.progress = ctk.CTkProgressBar(prog, height=10, corner_radius=5,
                                           progress_color="#3b82f6")
        self.progress.grid(row=0, column=0, sticky="ew")
        self.progress.set(0)
        # 进度文字不再写死宽度，避免窄窗口把进度条挤没
        self.progress_lbl = ctk.CTkLabel(prog, text="尚未识别名单", font=self.font_bold,
                                         anchor="e")
        self.progress_lbl.grid(row=0, column=1, sticky="e", padx=(14, 0))

        ctrl = ctk.CTkFrame(right, fg_color="transparent")
        ctrl.grid(row=2, column=0, sticky="ew", pady=10)
        ctrl.grid_columnconfigure(1, weight=1)
        self.btn_listen = self._btn(ctrl, "开始监听快捷键", "play", self.op_toggle_listen,
                                    kind="green", width=176)
        self.btn_listen.grid(row=0, column=0, sticky="w")
        self._btn(ctrl, "重置进度", "reset", self.op_reset, width=96).grid(
            row=0, column=1, sticky="w", padx=8)
        # 快捷键提示单独占一行：窗口变窄时只换行，不会被按钮挤掉
        self.hk_hint_lbl = ctk.CTkLabel(ctrl, text="", font=self.font_small,
                                        text_color=("gray45", "gray60"), anchor="w",
                                        justify="left")
        self.hk_hint_lbl.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))

        def _fit_hk_hint(e=None):
            try:
                w = right.winfo_width() - 16
                if w > 120 and abs(self.hk_hint_lbl.cget("wraplength") - w) > 12:
                    self.hk_hint_lbl.configure(wraplength=w)
            except Exception:
                pass
        right.bind("<Configure>", _fit_hk_hint)

        self.run_log = self._logbox(right, height=88)
        self.run_log.grid(row=3, column=0, sticky="ew")

        # 剩余文本复核区（识别后展开：确认有没有漏人）
        # 「知道了，隐藏」独立在框外、右对齐：这样它的底边和左列按钮行底边对齐，
        # 也不会占掉框内空间。
        self.leftover_box = ctk.CTkFrame(right, fg_color=("gray90", "#1a1e26"), corner_radius=10)
        self.leftover_title = ctk.CTkLabel(self.leftover_box, text="", font=self.font_bold,
                                           anchor="w", justify="left")
        self.leftover_title.pack(fill="x", padx=12, pady=(8, 2))
        self.leftover_text = ctk.CTkTextbox(self.leftover_box, height=44, font=self.font,
                                            wrap="word", fg_color=("gray86", "#12151b"))
        self.leftover_text.pack(fill="x", padx=12, pady=(0, 10))
        self.leftover_text.configure(state="disabled")
        self.leftover_box.grid(row=4, column=0, sticky="ew", pady=(8, 0))

        self.leftover_bar = ctk.CTkFrame(right, fg_color="transparent")
        self.leftover_bar.grid(row=5, column=0, sticky="ew", pady=(6, 0))
        self._btn(self.leftover_bar, "知道了，隐藏", "close", self._hide_leftover,
                  width=130).pack(side="right")
        # 复核框高度上限：约 96 物理像素。
        # 注意 CTkTextbox 的 height 是逻辑值、会被 DPI 放大，所以要按缩放系数反算，
        # 否则在高 DPI 下会占掉两三倍高度、把上面的名单挤没。
        # 队列能看几行由启动尺寸的托底值（FLOOR_H）保证，这里不再做复杂的自适应。
        def _cap_leftover(e=None):
            try:
                sc = self.leftover_text._apply_widget_scaling(1) or 1
                want = max(22, int(96 / sc))
                if abs(self.leftover_text.cget("height") - want) > 4:
                    self.leftover_text.configure(height=want)
            except Exception:
                pass
        right.bind("<Configure>", _cap_leftover, add="+")
        self.leftover_box.grid_remove()
        self.leftover_bar.grid_remove()

        self._update_line_mode_hint()
        self._rebuild_rows()
        self._update_progress()

    def _update_line_mode_hint(self):
        if self.line_mode_var.get():
            self.line_mode_hint.configure(text="每一行整体当一个名字去找")
        else:
            self.line_mode_hint.configure(text="整段文字里找名字（默认）")

    def op_toggle_line_mode(self):
        """勾选框状态一变就实时写入 config.json 的 line_mode。"""
        on = bool(self.line_mode_var.get())
        self._update_line_mode_hint()
        self.cfg["line_mode"] = on
        self._sync_editor_line_mode(on)
        try:
            target = get_default_config_path()
            data = try_parse_json(read_text(target)) or {}
            data["line_mode"] = on
            write_text(target, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
            self.run_log_msg(f"已记录识别方式：{'按行识别' if on else '整段检索'}")
        except Exception as e:
            self.run_log_msg(f"⚠ 保存识别方式失败：{e}")

    def _sync_editor_line_mode(self, on):
        """把配置页编辑框里那一行 line_mode 一起改掉，避免之后点保存把它改回去。"""
        label = SCALAR_LABELS["line_mode"]
        text = self._editor_text()
        new_line = f"{label} = {'true' if on else 'false'}"
        out, hit = [], False
        for line in text.splitlines():
            if line.split("=")[0].strip() == label:
                out.append(new_line)
                hit = True
            else:
                out.append(line)
        if not hit:
            return          # 编辑框里本来就没这一行，交给保存时用默认值补齐
        pos = self.editor._textbox.yview()[0]      # 尽量保持滚动位置
        self.editor.delete("1.0", "end")
        self.editor.insert("1.0", "\n".join(out) + "\n")
        self.editor._textbox.yview_moveto(pos)

    # ---------- 队列列表（单个 Canvas 自绘，缩放/增删都很快） ----------
    #
    # 为什么不用每行一个控件：一行 6 个 CTk 控件时，51 人就是 900+ 个 widget，
    # 拖动窗口改尺寸要几百毫秒，肉眼可见迟滞。改成在一个 Canvas 上画所有行，
    # 只有 1 个控件，拖动窗口时快 4~5 倍，而且圆角卡片/状态徽章外观不变。

    ROW_H = 54          # 行高（字号调大后同步加高）
    ROW_GAP = 8         # 行间距
    PAD_X = 8           # 左右留白

    def _build_rows_area(self, parent):
        """队列区：Canvas + 滚动条，替代 CTkScrollableFrame。"""
        holder = ctk.CTkFrame(parent, fg_color=("gray92", "#1a1e26"), corner_radius=10)
        self.rows_canvas = tk.Canvas(holder, bg=self._row_bg(), bd=0,
                                     highlightthickness=0, takefocus=0)
        self.rows_sb = ctk.CTkScrollbar(holder, command=self.rows_canvas.yview)
        self.rows_canvas.configure(yscrollcommand=self.rows_sb.set)
        self.rows_sb.pack(side="right", fill="y", padx=(0, 4), pady=4)
        self.rows_canvas.pack(side="left", fill="both", expand=True, padx=(4, 0), pady=4)
        self.rows_canvas.bind("<Configure>", self._on_rows_resize)
        self.rows_canvas.bind("<MouseWheel>", self._rows_wheel)
        # 点击命中：删除按钮 / 该行
        self.rows_canvas.bind("<Button-1>", self._rows_click)
        self.rows_canvas.bind("<Motion>", self._rows_motion)
        self.rows_canvas.bind("<Leave>", lambda e: self.rows_canvas.configure(cursor=""))
        return holder

    def _row_bg(self):
        return "#1a1e26" if ctk.get_appearance_mode() == "Dark" else "#e8eaed"

    def _row_card_bg(self):
        return "#242936" if ctk.get_appearance_mode() == "Dark" else "#ffffff"

    def _row_text_color(self):
        return ("#e8eaed" if ctk.get_appearance_mode() == "Dark" else "#1a1e26")

    def _row_sub_color(self):
        return ("#9ca3af" if ctk.get_appearance_mode() == "Dark" else "#4b5563")

    def _dark(self):
        return ctk.get_appearance_mode() == "Dark"

    def _rows_wheel(self, event):
        # 内容放得下时不响应滚轮，避免把名单滚出可视区留下一片空白
        if not self._rows_need_scroll():
            return "break"
        self.rows_canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
        return "break"

    def _rows_need_scroll(self):
        try:
            bbox = self.rows_canvas.bbox("all")
            need = (bbox[3] if bbox else 0)
            return need > self.rows_canvas.winfo_height() + 1
        except Exception:
            return False

    def _on_rows_resize(self, event):
        """窗口尺寸变化时重排行布局。

        直接重画：单 Canvas 重画只要几毫秒（51 行约 7ms），
        这样拖动窗口时名单是实时跟随的。之前的 after 防抖会让名单
        在拖动过程中不动、松手才跳过去，主观上就是"卡"。
        """
        self._redraw_rows()

    def _round_rect(self, cv, x1, y1, x2, y2, r, fill):
        """Canvas 上画圆角矩形（用平滑多边形近似）。"""
        pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
               x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        cv.create_polygon(pts, smooth=True, splinesteps=3, fill=fill, outline="")

    def _rows_click(self, event):
        """点删除按钮或行：命中测试靠坐标算（自绘没有独立控件）。"""
        uid = self._hit_delete(event.x, event.y)
        if uid is not None:
            self.op_del_row(uid)

    def _rows_motion(self, event):
        hit = self._hit_delete(event.x, event.y) is not None
        want = "hand2" if hit else ""
        if self.rows_canvas.cget("cursor") != want:
            self.rows_canvas.configure(cursor=want)

    def _hit_delete(self, x, y):
        """返回点击位置对应的 uid（只认删除按钮区域）。"""
        for uid, (x1, y1, x2, y2) in self._del_hit.items():
            if x1 <= x <= x2 and y1 <= y <= y2:
                return uid
        return None

    def _redraw_rows(self):
        """重画整个队列（单控件绘制，比几百个 widget 重排快得多）。"""
        cv = self.rows_canvas
        cw = cv.winfo_width() or 1
        ch = cv.winfo_height() or 1
        cv.delete("all")
        self._del_hit = {}
        order = self._display_order()
        if not order:
            cv.create_text(cw // 2, 40, text="粘贴名单后点击「识别名单」，名单会显示在这里",
                           fill=self._row_sub_color(), font=self.font)
            cv.configure(scrollregion=(0, 0, cw, ch))     # 不留可滚动空隙
            self._sync_scrollbar(False)
            return
        y = 6
        for i, p in enumerate(order):
            self._draw_row(cv, i, p, y, cw)
            y += self.ROW_H + self.ROW_GAP
        need = y + 6
        # 内容比可视区矮时，把滚动范围也设成可视高度：
        # 否则会出现"只有两行也有滚动条、还能往上滑出空白"的怪状态
        scroll_h = max(need, ch)
        cv.configure(scrollregion=(0, 0, cw, scroll_h))
        self._sync_scrollbar(need > ch + 1)
        if need <= ch + 1:
            cv.yview_moveto(0)

    def _sync_scrollbar(self, need_scroll):
        """内容放得下就隐藏滚动条（占位效果用 padx 不变，避免右侧跳动）。"""
        if not hasattr(self, "rows_sb"):
            return
        if need_scroll:
            if not self.rows_sb.winfo_ismapped():
                self.rows_sb.pack(side="right", fill="y", padx=(0, 4), pady=4)
        else:
            if self.rows_sb.winfo_ismapped():
                self.rows_sb.pack_forget()

    def _draw_row(self, cv, i, p, y, cw):
        bg = self._row_card_bg()
        self._round_rect(cv, self.PAD_X, y, cw - self.PAD_X - 1, y + self.ROW_H, 9, bg)
        cy = y + self.ROW_H // 2
        # 固定右端：状态徽章 + 删除按钮
        del_x2 = cw - self.PAD_X - 12
        del_x1 = del_x2 - 22
        badge_w = 80
        badge_x2 = del_x1 - 10
        badge_x1 = badge_x2 - badge_w
        self._del_hit[p["uid"]] = (del_x1 - 6, y + 6, del_x2 + 6, y + self.ROW_H - 6)
        # 自适应列宽：名字/QQ/附加按剩余空间等比分配，窄窗口也不会重叠
        avail = badge_x1 - 8
        w_no, w_name, w_qq, w_meta = 34, 0.34, 0.33, 0.33
        name_x = self.PAD_X + 12 + w_no
        rest = max(60, avail - name_x)
        c_name = int(rest * w_name)
        c_qq = int(rest * w_qq)
        # 序号
        cv.create_text(self.PAD_X + 20, cy, text=str(i + 1), anchor="w",
                       fill=self._row_sub_color(), font=self.font_badge)
        # 名字（发送失败标红）
        cv.create_text(name_x, cy, text=self._ellipsis(p["name"], c_name, self.font_row),
                       anchor="w", font=self.font_row,
                       fill="#ef4444" if p["status"] == ST_FAILED else self._row_text_color())
        # QQ 号
        qq = p["qq"] or "按姓名搜索"
        cv.create_text(name_x + c_name, cy, text=self._ellipsis(qq, c_qq, self.font_row),
                       anchor="w", font=self.font_row,
                       fill="#f59e0b" if not p["qq"] else self._row_sub_color())
        # 附加信息
        cnt = p.get("count") or 1
        meta = f"文本中 {cnt} 处" + ("・新增" if p.get("extra") else "")
        cv.create_text(name_x + c_name + c_qq, cy,
                       text=self._ellipsis(meta, rest - c_name - c_qq, self.font_badge),
                       anchor="w", font=self.font_badge,
                       fill="#38bdf8" if p.get("extra") else self._row_sub_color())
        # 状态徽章
        self._round_rect(cv, badge_x1, y + 13, badge_x2, y + self.ROW_H - 13, 7,
                         BADGE_COLORS[p["status"]])
        cv.create_text((badge_x1 + badge_x2) // 2, cy, text=p["status"],
                       fill="#ffffff", font=self.font_badge)
        # 删除按钮
        cv.create_text((del_x1 + del_x2) // 2, cy, text="✕",
                       fill="#f87171" if self._dark() else "#dc2626", font=self.font_row)

    def _ellipsis(self, text, max_px, font=None):
        """用真实字体测量、按像素宽度截断，避免文字互相压住（调字号后仍准确）。"""
        if not text:
            return ""
        font = font or self.font
        try:
            px = int(font.measure(text))          # CTkFont 继承自 tkinter.font.Font
        except Exception:
            try:
                fs = max(8, int(font.cget("size")))
            except Exception:
                fs = 15
            px = int(sum(fs if ord(ch) > 0x2E7F else fs * 0.55 for ch in text))
        if px <= max_px:
            return text
        n = len(text)
        while n > 1:
            try:
                w = int(font.measure(text[:n] + "…"))
            except Exception:
                w = int(n * max_px / max(1, len(text)))
            if w <= max_px:
                break
            n -= 1
        return text[:n] + "…"

    # 兼容旧调用名（原来每行是独立控件，现在是整块重绘）
    def _rebuild_rows(self):
        self._redraw_rows()

    def _relayout_rows(self):
        self._redraw_rows()

    def _renumber_rows(self):
        self._redraw_rows()

    def _remove_row_widget(self, uid):
        pass                     # 自绘没有独立控件需要销毁

    def _display_order(self):
        """显示顺序：发送失败的置顶，其余保持处理顺序。"""
        return sorted(self.queue, key=lambda p: 0 if p["status"] == ST_FAILED else 1)

    def _find_by_uid(self, uid):
        for j, p in enumerate(self.queue):
            if p["uid"] == uid:
                return j, p
        return None

    def _refresh_uid(self, uid):
        """刷新某一行（自绘模式下整块重画；行数不多时开销很小）。"""
        if self._find_by_uid(uid) is None:
            return
        self._redraw_rows()

    def _set_row_status(self, i):
        if not (0 <= i < len(self.queue)):
            return
        self._refresh_uid(self.queue[i]["uid"])

    # ---------- 配置页 ----------

    def _build_cfg_tab(self, f):
        f.grid_columnconfigure(0, weight=1)
        f.grid_rowconfigure(2, weight=1)

        top = ctk.CTkFrame(f, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew")
        top.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(top, text="配置文件", font=self.font_bold).grid(row=0, column=0, sticky="w")
        self.path_var = ctk.StringVar()
        e = ctk.CTkEntry(top, textvariable=self.path_var, height=32, font=self.font_mono,
                         fg_color=("gray94", "#161920"), state="readonly",
                         text_color=("gray35", "gray60"))
        e.grid(row=0, column=1, sticky="ew", padx=10)
        self._btn(top, "打开所在文件夹", "folder", self.op_open_dir, width=150).grid(
            row=0, column=2)
        self.btn_delete_data = self._btn(top, "删除本软件全部数据", "trash",
                                         self.op_delete_data, kind="danger",
                                         width=152, small=True)
        self.btn_delete_data.grid(row=0, column=3, padx=(8, 0), pady=3)

        ctk.CTkLabel(f, text="直接改下面内容再点「保存配置」即可；"
                             "「名字 QQ号」一行一个人；# 开头是注释",
                     font=self.font_small, text_color=("gray45", "gray60"), anchor="w"
                     ).grid(row=1, column=0, sticky="ew", pady=(10, 4))

        self.editor = ctk.CTkTextbox(f, font=self.font_mono, wrap="word",
                                     fg_color=("gray94", "#161920"))
        self.editor.grid(row=2, column=0, sticky="nsew")

        bar = ctk.CTkFrame(f, fg_color="transparent")
        bar.grid(row=3, column=0, sticky="ew", pady=10)
        self.btn_read = self._btn(bar, "重新读取", "download", self.op_read, width=110)
        self.btn_read.pack(side="left")
        self.btn_save = self._btn(bar, "保存配置", "save", self.op_save, kind="green", width=110)
        self.btn_save.pack(side="left", padx=8)
        self.cfg_status_lbl = ctk.CTkLabel(bar, text="", font=self.font_bold)
        self.cfg_status_lbl.pack(side="left", padx=14)

        self.cfg_log = self._logbox(f, height=76)
        self.cfg_log.grid(row=4, column=0, sticky="ew")

    def _build_help_tab(self, f):
        t = ctk.CTkTextbox(f, font=self.font, wrap="word",
                           fg_color=("gray94", "#161920"))
        t.pack(fill="both", expand=True)
        t.insert("1.0", HELP_TEXT)
        t.configure(state="disabled")

    # ---------- 日志 / 状态 ----------

    def run_log_msg(self, msg):
        self._log(self.run_log, msg)

    def cfg_log_msg(self, msg):
        self._log(self.cfg_log, msg)

    def _set_cfg_status(self, msg, ok):
        color = ("#15803d", "#4ade80") if ok else ("#b91c1c", "#f87171")
        self.cfg_status_lbl.configure(text=("✓ " if ok else "✗ ") + msg, text_color=color)

    def _update_progress(self):
        n = len(self.queue)
        hk = self.cfg.get("hotkeys") or {}

        def key(a, d):
            return str(hk.get(a) or d).upper()
        self.hk_hint_lbl.configure(
            text=f"{key('next','f8')} 下一位  {key('repeat','f7')} 重输  "
                 f"{key('skip','f9')} 跳过  {key('failed','f10')} 发送失败  "
                 f"{key('quit','ctrl+f12')} 结束")
        n_failed = sum(1 for p in self.queue if p["status"] == ST_FAILED)
        self.count_lbl.configure(
            text=f"共 {n} 人" + (f"（{n_failed} 人发送失败）" if n_failed else ""))
        self.progress.set((self.idx / n) if n else 0)
        if not n:
            self.progress_lbl.configure(text="尚未识别名单")
        elif self.idx >= n:
            self.progress_lbl.configure(text=f"全部完成（共 {n} 人）")
        else:
            p = self.queue[self.idx]
            self.progress_lbl.configure(
                text=f"进度 {self.idx}/{n}　下一位：{p['name']}（{p['qq'] or '按姓名搜索'}）")

    # ---------- 事件轮询（快捷键钩子线程 -> 界面线程） ----------

    def _poll(self):
        try:
            while True:
                ev = self.ui_queue.get_nowait()
                kind = ev[0]
                if kind == "hk":
                    self._on_hotkey(ev[1])
                elif kind == "typed":
                    p = ev[1]
                    item = self._find_by_uid(p.get("uid"))
                    if item is not None and item[1]["status"] == ST_TYPING:
                        item[1]["status"] = ST_DONE
                        self._refresh_uid(p.get("uid"))
                    self.run_log_msg(
                        f"已输入：{p['name']} → {p['qq'] or '姓名'}"
                        f"（提醒话术已复制，Ctrl+V 粘贴）")
                elif kind == "type_error":
                    item = self._find_by_uid(ev[1].get("uid"))
                    if item is not None and item[1]["status"] == ST_TYPING:
                        item[1]["status"] = ST_PENDING
                        self._refresh_uid(ev[1].get("uid"))
                    self.run_log_msg(f"⚠ 输入失败：{ev[1]['msg']}")
                elif kind == "clip_error":
                    self.run_log_msg(f"⚠ 复制提醒话术失败：{ev[1]}")
                elif kind == "error":
                    self.run_log_msg(f"⚠ {ev[1]}")
        except _queue.Empty:
            pass
        self.after(80, self._poll)

    def _on_hotkey(self, action):
        if not self.listening:
            return
        if action == "next":
            self._advance()
        elif action == "repeat":
            self._repeat()
        elif action == "skip":
            self._skip()
        elif action == "failed":
            self._mark_failed()
        elif action == "quit":
            self._stop_listen("收到结束快捷键")
            messagebox.showinfo(APP_TITLE, self._summary_text(), parent=self)

    # ---------- 提醒页操作 ----------

    def op_import_file(self):
        p = filedialog.askopenfilename(parent=self, title="选择名单文件",
                                       filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")])
        if not p:
            return
        try:
            lines = read_text(p).splitlines()
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"读取文件失败：{e}", parent=self)
            return
        self.list_text.delete("1.0", "end")
        self.list_text.insert("1.0", "\n".join(lines))
        self.run_log_msg(f"已从文件导入 {len(lines)} 行：{os.path.basename(p)}")

    def op_clear_list(self):
        self.list_text.delete("1.0", "end")
        self._clear_marks()
        self._hide_leftover()

    def op_parse(self):
        text = self.list_text.get("1.0", "end-1c").replace("\r\n", "\n").replace("\r", "\n")
        qq_map = {str(k): str(v) for k, v in (self.cfg.get("qq_map") or {}).items()}
        if not qq_map:
            messagebox.showwarning(APP_TITLE, "配置里的 QQ 映射表是空的，"
                                              "请先到「配置」页填入「名字 QQ号」。", parent=self)
            return
        if self.line_mode_var.get():
            self._parse_by_lines(text, qq_map)
        else:
            self._parse_by_scan(text, qq_map)

    def _apply_hits(self, hits, text):
        """把命中结果装进队列、画到界面。"""
        self.queue = [{"uid": self._next_uid(), "name": h["name"], "qq": h["qq"],
                       "status": ST_PENDING, "count": h["count"],
                       "extra": h.get("extra", False)}
                      for h in hits]
        self.idx = 0
        self._rebuild_rows()
        self._mark_list_text(text, hits)
        return len(self.queue)

    def _parse_by_scan(self, text, qq_map):
        """默认：拿名单里的名字去整段文字里检索。"""
        hits, leftover, missed = scan_text(text, qq_map)
        n = self._apply_hits(hits, text)
        n_extra = sum(1 for p in self.queue if p.get("extra"))
        n_named = sum(1 for p in self.queue if not p["qq"])
        msg = f"识别到 {n} 人（{sum(h['count'] for h in hits)} 处匹配）"
        if n_extra:
            msg += f"，其中 {n_extra} 人是文本里新增的"
        if n_named:
            msg += f"，{n_named} 人没有 QQ 号将按姓名搜索"
        self.run_log_msg(msg)
        self._update_progress()
        if not n:
            self._hide_leftover()
            messagebox.showinfo(APP_TITLE,
                                "没有在文本里检索到任何名单里的名字。\n"
                                "请确认名单已经粘进来了，或到「配置」页补上对应的人。",
                                parent=self)
            return
        if missed:
            self.run_log_msg("本次文本里没出现的名单成员：" + "、".join(missed[:20])
                             + ("…" if len(missed) > 20 else ""))
        self._show_leftover(leftover, n)

    def _parse_by_lines(self, text, qq_map):
        """按行识别：一行 = 一个人。"""
        hits, unmatched, missed = scan_lines(text, qq_map)
        n = self._apply_hits(hits, text)
        n_extra = sum(1 for p in self.queue if p.get("extra"))
        msg = f"按行识别到 {n} 人"
        if n_extra:
            msg += f"，其中 {n_extra} 人是行内指定/新增的"
        if unmatched:
            msg += f"，{len(unmatched)} 行没匹配到"
        self.run_log_msg(msg)
        self._update_progress()
        if not n and not unmatched:
            self._hide_leftover()
            messagebox.showinfo(APP_TITLE, "名单是空的，先粘贴内容再识别。", parent=self)
            return
        if missed:
            self.run_log_msg("名单里没出现的成员：" + "、".join(missed[:20])
                             + ("…" if len(missed) > 20 else ""))
        self._show_unmatched(unmatched, n)

    def _clear_marks(self):
        try:
            self.list_text.tag_remove("hit", "1.0", "end")
        except Exception:
            pass

    def _mark_list_text(self, text, hits):
        """在左侧输入框里把命中的名字高亮（不改动文本本身）。"""
        self.list_text.delete("1.0", "end")
        self.list_text.insert("1.0", text)
        self._clear_marks()
        try:
            self.list_text.tag_config("hit", background="#f59e0b", foreground="#1a1e26")
        except Exception:
            return
        for h in hits:
            # 高亮区间直接用 scan 记录的命中位置：与识别逻辑完全一致，
            # 不会把互为子串的短名字（张三）亮进长名字（张三三）里
            for i, j in h.get("pos", []):
                line = text.count("\n", 0, i) + 1
                col = i - (text.rfind("\n", 0, i) + 1)
                self.list_text.tag_add("hit", f"{line}.{col}", f"{line}.{col + (j - i)}")

    def _show_leftover(self, leftover, n_hit):
        """展示未被标记的剩余文本，让用户确认有没有漏人。"""
        if not leftover or not CJK_NAME_RE.search(leftover):
            self._hide_leftover()
            self.run_log_msg("名单里的人全部检索到了，没有多余文字需要复核。")
            return
        self.leftover_title.configure(
            text=f"⚠ 这些文字没有被任何名单成员匹配到（共 {n_hit} 人已识别）——\n"
                 f"请看一眼有没有漏掉的人（比如 OCR 认错字导致名字对不上）：")
        self._set_leftover_body(leftover)
        self.run_log_msg("有未能匹配的文字，已在下方列出，请确认是否漏人。")

    def _show_unmatched(self, unmatched, n_hit):
        """按行识别时：逐行列出没匹配到的内容（带原行号，方便对照找错字）。"""
        if not unmatched:
            self._hide_leftover()
            self.run_log_msg("每一行都匹配上了，没有需要复核的内容。")
            return
        self.leftover_title.configure(
            text=f"⚠ 下面这些行没有在名单里匹配到（共 {n_hit} 人已识别）——\n"
                 f"一行就是一个人，可能是 OCR 认错字或不在名单里，请对照原行号核对：")
        body = "\n".join(f"第 {u['line']} 行：{u['text']}" for u in unmatched)
        self._set_leftover_body(body)
        self.run_log_msg(f"有 {len(unmatched)} 行没匹配到，已在下方按行列出。")

    def _set_leftover_body(self, body):
        self.leftover_text.configure(state="normal")
        self.leftover_text.delete("1.0", "end")
        self.leftover_text.insert("1.0", body)
        self.leftover_text.configure(state="disabled")
        self.leftover_box.grid()
        self.leftover_bar.grid()

    def _hide_leftover(self):
        self.leftover_box.grid_remove()
        self.leftover_bar.grid_remove()

    def op_del_row(self, uid):
        """按稳定 uid 删除一行（连点删除时下标会变，所以不能用下标）。"""
        pos = next((j for j, p in enumerate(self.queue) if p["uid"] == uid), None)
        if pos is None:
            return
        p = self.queue[pos]
        name = p["name"]
        # 删除位置在当前处理指针之前时，指针要跟着前移，否则会跳人
        if pos < self.idx:
            self.idx -= 1
        del self.queue[pos]
        self._remove_row_widget(uid)
        if not self.queue:
            self._rebuild_rows()
        else:
            # 只销毁被删的那一行 + 刷新序号，不重排其它行，所以连点也不会闪
            self._renumber_rows()
        self._update_progress()
        self.run_log_msg(f"已删除：{name}")

    def op_reset(self):
        for p in self.queue:
            p["status"] = ST_PENDING
        self.idx = 0
        self._rebuild_rows()
        self._update_progress()

    def _submit_type(self):
        p = self.queue[self.idx]
        p["status"] = ST_TYPING
        self._set_row_status(self.idx)
        if self.worker:
            self.worker.submit_type({"uid": p["uid"], "name": p["name"],
                                     "qq": p["qq"], "cfg": self.cfg})

    def _advance(self):
        while True:
            if self.idx >= len(self.queue):
                self.run_log_msg("名单已全部处理完；可点击「重置进度」再来一轮，或停止监听。")
                self._update_progress()
                return
            p = self.queue[self.idx]
            if p["qq"] is None and not self.cfg.get("search_by_name_fallback", True):
                p["status"] = ST_SKIPPED
                self._set_row_status(self.idx)
                self.run_log_msg(f"跳过 {p['name']}（没有 QQ 号，且未开启按姓名搜索）")
                self.idx += 1
                continue
            break
        self._submit_type()
        self.idx += 1
        self._update_progress()

    def _repeat(self):
        if self.idx == 0:
            self.run_log_msg("还没开始输入过，直接按下一位快捷键即可。")
            return
        self.idx -= 1
        p = self.queue[self.idx]
        self._submit_type()
        self.idx += 1
        self._update_progress()
        self.run_log_msg(f"重新输入：{p['name']} → {p['qq'] or '姓名'}")

    def _skip(self):
        if self.idx >= len(self.queue):
            self.run_log_msg("名单已全部处理完。")
            return
        p = self.queue[self.idx]
        p["status"] = ST_SKIPPED
        self._set_row_status(self.idx)
        self.idx += 1
        self.run_log_msg(f"已跳过：{p['name']}")
        self._advance()

    def _current_person(self):
        """刚刚输入过的那个人（F7 重输、F8 下一位，处理完都停在 idx-1）。"""
        if not self.queue:
            return None
        if self.idx > 0 and self.idx - 1 < len(self.queue):
            return self.queue[self.idx - 1]
        return None

    def _mark_failed(self):
        """把刚刚输入过的那位标记为「发送失败」并顶置，方便单独处理。

        再按一次可以取消标记（按错了不用慌）。只改状态、不动处理顺序，
        所以不会影响 F8 的进度。
        """
        p = self._current_person()
        if p is None:
            self.run_log_msg("还没输入过任何人；先按下一位快捷键再标记。")
            return
        if p["status"] == ST_FAILED:
            p["status"] = ST_DONE
            self.run_log_msg(f"已取消「发送失败」标记：{p['name']}")
        else:
            p["status"] = ST_FAILED
            self.run_log_msg(f"已标记发送失败并置顶：{p['name']}"
                             f"（{p['qq'] or '按姓名搜索'}）"
                             f"，再按一次可取消标记")
        self._relayout_rows()
        self._update_progress()

    def _summary_text(self):
        done = [p["name"] for p in self.queue if p["status"] == ST_DONE]
        skipped = [p["name"] for p in self.queue if p["status"] == ST_SKIPPED]
        failed = [p["name"] for p in self.queue if p["status"] == ST_FAILED]
        s = f"共 {len(self.queue)} 人：已输入 {len(done)} 人，跳过 {len(skipped)} 人"
        s += f"，发送失败 {len(failed)} 人。" if failed else "。"
        if failed:
            s += "\n发送失败：" + "、".join(failed)
        if skipped:
            s += "\n跳过：" + "、".join(skipped)
        return s

    def _set_listen_ui(self):
        if self.listening:
            self.btn_listen.configure(text="停止监听", image=self.icon("stop"),
                                      fg_color="#ef4444", hover_color="#dc2626")
        else:
            self.btn_listen.configure(text="开始监听快捷键", image=self.icon("play"),
                                      fg_color="#22c55e", hover_color="#16a34a")

    def op_toggle_listen(self):
        if self.listening:
            self._stop_listen("手动停止")
            return
        if not self.queue:
            messagebox.showinfo(APP_TITLE, "请先粘贴名单并点击「识别名单」。",
                                parent=self)
            return
        if keyboard is None:
            messagebox.showerror(APP_TITLE, f"缺少 keyboard 库：{_IMPORT_ERROR}", parent=self)
            return
        hk = self.cfg.get("hotkeys") or {}
        handles = []
        try:
            for action, default in (("next", "f8"), ("repeat", "f7"), ("skip", "f9"),
                                    ("failed", "f10"), ("quit", "ctrl+f12")):
                combo = str(hk.get(action) or default).strip().lower()
                handles.append(keyboard.add_hotkey(
                    combo, lambda a=action: self.ui_queue.put(("hk", a))))
        except Exception as e:
            for h in handles:
                try:
                    keyboard.remove_hotkey(h)
                except Exception:
                    pass
            messagebox.showerror(
                APP_TITLE,
                f"注册快捷键失败：{e}\n\n请检查配置里 hotkeys 的按键写法\n（如 f8、ctrl+f12、alt+q），或换一组按键。",
                parent=self)
            return
        self._hk_handles = handles
        self.listening = True
        self._set_listen_ui()
        self.run_log_msg("开始监听。点一下 QQ 左上角的搜索框，按下一位快捷键开始。")
        self._update_progress()

    def _stop_listen(self, reason):
        for h in self._hk_handles:
            try:
                keyboard.remove_hotkey(h)
            except Exception:
                pass
        self._hk_handles = []
        self.listening = False
        self._set_listen_ui()
        self.run_log_msg(f"已停止监听（{reason}）。")
        self._update_progress()

    # ---------- 配置页操作（执行期间冻结按钮，校验通过后解锁） ----------

    def _cfg_busy(self, busy):
        state = "disabled" if busy else "normal"
        for attr in self.CFG_BUTTONS:
            getattr(self, attr).configure(state=state)

    def _editor_text(self):
        return self.editor.get("1.0", "end-1c")

    def _startup_load(self):
        try:
            target, text, msgs = startup_config()
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"初始化配置失败：{e}", parent=self)
            return
        self.current_config_path = target
        self.path_var.set(target)
        data = try_parse_json(text) or {}
        disp, dropped = _displayable(data)
        self.editor.delete("1.0", "end")
        self.editor.insert("1.0", config_to_lines(disp))
        if dropped:
            self.cfg_log_msg("配置文件里有界面不支持的字段，已忽略：" + "、".join(dropped))
        self.cfg = merged_config(data)
        # 勾选框是在配置加载之前建的（用的是默认值），这里按真实配置同步一次，
        # 否则上次勾选的「按行识别」每次启动都会丢
        try:
            self.line_mode_var.set(bool(self.cfg.get("line_mode", False)))
            self._update_line_mode_hint()
        except Exception:
            pass
        for m in msgs:
            self.cfg_log_msg(m)
        ok = not any(("失败" in m or "无法" in m) for m in msgs)
        self._set_cfg_status("已自动读取配置" if ok else "启动读取有警告，见下方日志", ok)
        self._update_progress()

    def op_read(self):
        self._cfg_busy(True)
        try:
            target = get_default_config_path()
            text, msgs = ensure_config(target)
            if text is None:
                raise RuntimeError("配置文件无法读取也无法生成")
            for m in msgs:
                self.cfg_log_msg(m)
            disk = read_text(target)
            if disk != text:
                write_text(target, text)
                disk = read_text(target)
            data = try_parse_json(disk)
            if data is None:
                raise RuntimeError("配置文件不是有效的 JSON")
            disp, dropped = _displayable(data)
            self.editor.delete("1.0", "end")
            self.editor.insert("1.0", config_to_lines(disp))
            if dropped:
                self.cfg_log_msg("配置文件里有界面不支持的字段，已忽略：" + "、".join(dropped))
            parsed, err = parse_config_lines(self._editor_text())
            if err:
                raise RuntimeError(f"校验失败：{err}")
            if _norm_dict(parsed) != _norm_dict(disp):
                raise RuntimeError("校验失败：编辑框内容与配置文件不一致")
            self.cfg = merged_config(data)
            self.current_config_path = target
            # 配置里的 line_mode 可能被改过，同步到勾选框（不触发写盘）
            try:
                self.line_mode_var.set(bool(self.cfg.get("line_mode", False)))
                self._update_line_mode_hint()
            except Exception:
                pass
            self._set_cfg_status("已重新读取", True)
            self._update_progress()
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"读取配置失败：\n{e}", parent=self)
            self._set_cfg_status(f"读取失败：{e}", False)
        finally:
            self._cfg_busy(False)

    def op_save(self):
        self._cfg_busy(True)
        try:
            target = get_default_config_path()
            text = self._editor_text()
            parsed, err = parse_config_lines(text)
            if err:
                raise RuntimeError(f"{err}（未保存）")
            write_text(target, json.dumps(parsed, ensure_ascii=False, indent=2) + "\n")
            disk = read_text(target)
            data = try_parse_json(disk)
            if data is None or _norm_dict(data) != _norm_dict(parsed):
                raise RuntimeError("保存后校验失败：磁盘文件与编辑框内容不一致")
            self.cfg = merged_config(data)
            # 编辑框里删掉/改了 line_mode 那行时，勾选框要跟着配置走
            try:
                self.line_mode_var.set(bool(self.cfg.get("line_mode", False)))
                self._update_line_mode_hint()
            except Exception:
                pass
            self.cfg_log_msg(f"已保存：{target}")
            self._set_cfg_status("已保存", True)
            self._update_progress()
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"保存配置失败：\n{e}", parent=self)
            self._set_cfg_status(f"保存失败：{e}", False)
        finally:
            self._cfg_busy(False)

    def op_open_dir(self):
        d = get_data_dir()
        try:
            os.makedirs(d, exist_ok=True)
            if os.path.isfile(get_default_config_path()):
                # 选中配置文件，方便直接看/备份
                subprocess.Popen(["explorer", "/select,", get_default_config_path()])
            else:
                os.startfile(d)          # noqa: S606 - Windows 专用
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"打开文件夹失败：\n{e}", parent=self)

    def op_delete_data(self):
        """删除整个数据目录并退出程序（二级确认）。"""
        d = get_data_dir()
        if not messagebox.askyesno(
                APP_TITLE,
                "确定要删除本软件的全部数据吗？\n\n"
                "会删掉配置里保存的名单 QQ 映射，删掉就不可恢复了。\n"
                "下次再用本软件时会重新生成一样的文件夹。\n\n"
                "确认删除？", parent=self, default="no"):
            return
        if not messagebox.askyesno(
                APP_TITLE,
                f"最后确认一次：删除「{d}」？\n\n该操作不可恢复。",
                parent=self, default="no"):
            return
        self._cfg_busy(True)
        try:
            if self.listening:
                self._stop_listen("删除数据并退出")
            if os.path.isdir(d):
                shutil.rmtree(d)
            if os.path.isdir(d):
                raise RuntimeError("目录仍然存在，可能被其他程序占用")
        except Exception as e:
            self._cfg_busy(False)
            messagebox.showerror(APP_TITLE, f"删除失败：\n{e}", parent=self)
            return
        messagebox.showinfo(APP_TITLE, "已删除，程序即将退出。", parent=self)
        self._on_close()

    # ---------- 关闭 ----------

    def _on_close(self):
        try:
            if self.listening:
                self._stop_listen("退出程序")
        except Exception:
            pass
        self.destroy()


# ---------------- 自检（打包 exe 后可命令行验证：QQReminder.exe --selftest） ----------------

def run_selftest():
    import tempfile
    lines, ok = [], True
    try:
        with tempfile.TemporaryDirectory() as td:
            cfg_path = os.path.join(td, "sub", CONFIG_NAME)

            text, msgs = ensure_config(cfg_path)
            ok &= try_parse_json(text) is not None and os.path.isfile(cfg_path)
            lines += msgs

            write_text(cfg_path, "{ 这不是合法 JSON")
            text2, msgs2 = ensure_config(cfg_path)
            ok &= try_parse_json(text2) is not None
            lines += msgs2

            # 整段文本检索：名字出现在杂乱的文字里也能找到，且不动原文本
            qq_map = {"张三": "10001", "李四": "10002", "赵六": "10004", "王五": "10003"}
            text = ("未完成名单（共 12 人）\n"
                    "飞机和嘎就看四李四微博张张三三就开始三张三张北京张三控股\n"
                    "1. 张三\n3、赵六 20231101\n新成员，999888777\n")
            hits, leftover, missed = scan_text(text, qq_map)
            got = [(h["name"], h["qq"], h["count"]) for h in hits]
            expect = [("张三", "10001", 4), ("李四", "10002", 1), ("赵六", "10004", 1),
                      ("新成员", "999888777", 1)]
            ok &= (got == expect)
            lines.append(f"scan -> {got}")
            # 学号不能被当成 QQ 号（8 位学号恰好符合 QQ 号长度）
            ok &= all(q != "20231101" for _, q, _ in got)
            # 名单里有、文本里没出现的人必须报告在 missed 里（不能是空断言）
            ok &= (missed == ["王五"])
            lines.append(f"missed -> {missed}")
            # 剩余文本：已标记位置用空格顶替，所以不会把相邻残字拼成假名字，
            # 已识别的人名不应再完整出现在剩余文本里；标题行也应被丢掉
            ok &= not any(n in leftover for n, _, _ in got)
            ok &= ("控股" in leftover)          # 真正没匹配到的尾巴要留着给用户看
            ok &= ("未完成名单" not in leftover)
            lines.append(f"leftover -> {leftover!r}")
            # 全部命中时不该有剩余文本
            hits2, leftover2, _ = scan_text("张三 李四 赵六", qq_map)
            ok &= (leftover2 == "")
            lines.append(f"full-hit leftover -> {leftover2!r}")

            # 名字互为子串时，长名字优先，短的不能把长的吃掉
            sub_map = {"张三": "111", "张三三": "222"}
            hs, _, _ = scan_text("张三三来了", sub_map)
            ok &= ([h["name"] for h in hs] == ["张三三"])
            hs, _, _ = scan_text("张三来了", sub_map)
            ok &= ([h["name"] for h in hs] == ["张三"])
            lines.append("substring names -> long-first ok")

            # 命中顺序与配置里的名单顺序一致
            om = {"丙": "1", "甲": "2", "乙": "3"}
            hs, _, _ = scan_text("乙 丙 甲", om)
            ok &= ([h["name"] for h in hs] == ["丙", "甲", "乙"])
            lines.append("hit order -> follows config order")

            # 名字后的行内 QQ 号要生效（中文/英文逗号都行，不管名字在第几个位置）
            ok &= (scan_text("张三, 999888777", qq_map)[0][0]["qq"] == "999888777")
            ok &= (scan_text("前面有字 李四，888777999", qq_map)[0][0]["qq"] == "888777999")
            # 但空格后面的数字（学号）不能当 QQ 号
            ok &= (scan_text("张三 20231101", qq_map)[0][0]["qq"] == "10001")
            lines.append("explicit qq after name -> ok")

            # 名字后面的括号备注跟着名字一起吃掉，不算「可疑漏人文字」
            hs, lo, _ = scan_text("王五（已请假） 剩下这些字", {"王五": "111"})
            ok &= ("（已请假）" not in lo and "剩下这些字" in lo)
            lines.append(f"note after name -> {lo!r}")

            # 独占一行的「名字 QQ号」= 用户显式指定，优先于配置里的映射
            hs, _, _ = scan_text("张三 88888888", qq_map)
            ok &= (len(hs) == 1 and hs[0]["qq"] == "88888888")
            hs, _, _ = scan_text("张三, 77777777", qq_map)         # 逗号形式同样生效
            ok &= (len(hs) == 1 and hs[0]["qq"] == "77777777")
            # 行内「名字 学号」不算 map 条目（学号不是 QQ 号）
            hs, _, _ = scan_text("1. 张三 20231101", qq_map)
            ok &= (len(hs) == 1 and hs[0]["qq"] == "10001")
            # 写成「名字, 20231101」时用户是明确的，就按 QQ 号用
            hs, _, _ = scan_text("张三, 20231101", qq_map)
            ok &= (len(hs) == 1 and hs[0]["qq"] == "20231101")
            # 配置里没有、但以 map 格式写进来的 -> 作为额外的人一起提醒
            hs, _, _ = scan_text("新成员 66666666", qq_map)
            ok &= (len(hs) == 1 and hs[0]["name"] == "新成员"
                   and hs[0]["qq"] == "66666666" and hs[0].get("extra"))
            # 名字和 QQ 号分处两行不能被粘成一条（曾因 \s 匹配换行而出错）
            hs, _, _ = scan_text("张三\n88888888", qq_map)
            ok &= (len(hs) == 1 and hs[0]["qq"] == "10001")
            lines.append("map-line format -> ok")

            # 优先级：行内「, QQ号」 > 独占一行的 map 条目 > 配置
            hs, _, _ = scan_text("张三 88888888\n正文 张三，55555555", qq_map)
            ok &= (len(hs) == 1 and hs[0]["qq"] == "55555555")
            lines.append("qq priority -> inline > map line > config")

            # 「发送失败」状态与置顶排序
            ok &= (ST_FAILED in BADGE_COLORS)
            ok &= ("failed" in HOTKEY_KEYS and "failed" in DEFAULT_CONFIG["hotkeys"])
            ok &= ("failed" in HOTKEY_LABELS)

            # 按行识别：一行 = 一个人
            lm = {"张三": "10001", "李四": "10002", "王五": "10003"}
            lh, lu, lmiss = scan_lines("1. 张三\n李四（已请假）\n王五\n尽快发货未啊付款啊",
                                       lm)
            ok &= ([h["name"] for h in lh] == ["张三", "李四", "王五"])
            ok &= ([h["qq"] for h in lh] == ["10001", "10002", "10003"])
            ok &= (len(lu) == 1 and lu[0]["line"] == 4
                   and lu[0]["text"] == "尽快发货未啊付款啊")
            # 同一行两个人不应被悄悄当成一个人
            _, lu2, _ = scan_lines("张三 李四", lm)
            ok &= (len(lu2) == 1)
            # 行内「名字 qq」优先；空格后的学号仍按 map
            lh, _, _ = scan_lines("张三 88888888", lm)
            ok &= (len(lh) == 1 and lh[0]["qq"] == "88888888")
            lh, _, _ = scan_lines("王五 20231101", lm)
            ok &= (len(lh) == 1 and lh[0]["qq"] == "10003")
            # 名字出现两处也只算一个人（一行一人）
            lh, _, _ = scan_lines("张三\n张三", lm)
            ok &= (len(lh) == 1)
            lines.append(f"line-mode -> hits={len(lh)} unmatched={len(lu)}")
            ok &= ("line_mode" in SCALAR_KEYS and "line_mode" in BOOL_KEYS
                   and "line_mode" in DEFAULT_CONFIG)

            # 配置编辑框（中文标签 / 名字 QQ）<-> JSON 往返
            cfg0 = merged_config({})
            cfg0["qq_map"] = {"张三": "10001", "李四": "10002"}
            cfg0["message_template"] = "{name}，你好！ a = b"
            back, err = parse_config_lines(config_to_lines(cfg0))
            ok &= (err is None and _norm_dict(back) == _norm_dict(cfg0))
            lines.append(f"editor roundtrip err={err}")
            # 中文标签要能认出来
            ok &= "输入下一位被提醒人qq = f8" in config_to_lines(cfg0)
            ok &= "张三 10001" in config_to_lines(cfg0)
            # 报错场景
            _, e2 = parse_config_lines("输入下一位被提醒人qq = ")
            ok &= (e2 is not None)
            _, e3 = parse_config_lines("名字qq写错了")
            ok &= (e3 is not None)
            _, e4 = parse_config_lines("模拟打字每个字符的间隔（秒） = 太快了")
            ok &= (e4 is not None)
            lines.append(f"editor error checks -> {e2 is not None}, {e3 is not None}, "
                         f"{e4 is not None}")
    except Exception as e:
        ok = False
        lines.append(f"EXCEPTION: {e!r}")

    # 数据目录探测（只验证路径拼接，不碰真实数据）
    try:
        d = get_data_dir()
        p = get_default_config_path()
        ok &= (os.path.dirname(p) == d and os.path.basename(p) == CONFIG_NAME)
        ok &= (APP_DIR_NAME in d)
        lines.append(f"data dir -> {d}")
    except Exception as e:
        ok = False
        lines.append(f"data dir EXCEPTION: {e!r}")

    result = ("SELFTEST PASS" if ok else "SELFTEST FAIL") + "\n" + "\n".join(lines) + "\n"
    try:
        with open(os.path.join(get_app_dir(), "selftest_result.txt"), "w", encoding="utf-8") as f:
            f.write(result)
    except Exception:
        pass
    if sys.stdout:
        try:
            print(result)
        except Exception:
            pass
    return 0 if ok else 1


# ---------------- 入口 ----------------

def is_admin():
    """当前进程是否以管理员权限运行（非 Windows 返回 True）。"""
    if ctypes is None:
        return True
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return True


def relaunch_as_admin():
    """以管理员权限重新启动自己。成功接管则返回 True（本进程应退出）。"""
    if ctypes is None:
        return False
    try:
        args = [a for a in sys.argv[1:] if a != ELEVATED_FLAG] + [ELEVATED_FLAG]
        if getattr(sys, "frozen", False):
            exe = sys.executable
            params = subprocess.list2cmdline(args)
        else:
            exe = sys.executable
            params = subprocess.list2cmdline([os.path.abspath(__file__)] + args)
        # ShellExecuteW 的 runas 会弹 UAC；用户取消时返回值 <= 32
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", exe, params, get_app_dir(), 1)
        return int(rc) > 32
    except Exception:
        return False


def _install_excepthook():
    def _hook(t, v, tb):
        try:
            import traceback
            with open(os.path.join(get_app_dir(), LOG_NAME), "a", encoding="utf-8") as f:
                traceback.print_exception(t, v, tb, file=f)
        except Exception:
            pass
    sys.excepthook = _hook


def main():
    if "--selftest" in sys.argv:
        sys.exit(run_selftest())
    if "--no-elevate" not in sys.argv:
        # 双击启动时若不是管理员，就先提权重启自己：此过程还没创建任何窗口，
        # 用户只会看到 UAC 弹窗，点「是」之后出现的主界面已经是管理员权限了，
        # 因此感受不到「重启」。ELEVATED_FLAG 用来防止极端情况下反复重启。
        if not is_admin() and ELEVATED_FLAG not in sys.argv:
            if relaunch_as_admin():
                return
            # 用户拒绝了 UAC：给一个降级运行的选项，而不是直接退出
            try:
                import tkinter as _tk
                from tkinter import messagebox as _mb
                r = _tk.Tk()
                r.withdraw()
                cont = _mb.askyesno(
                    APP_TITLE,
                    "本程序用管理员权限运行才能稳定模拟按键，你刚刚取消了授权。\n\n"
                    "仍要以普通权限运行吗？\n"
                    "「是」：继续运行，但全局快捷键/模拟按键可能失效\n"
                    "「否」：退出程序")
                r.destroy()
            except Exception:
                cont = False
            if not cont:
                return
    _install_excepthook()
    missing = []
    if ctk is None:
        missing.append("customtkinter / pillow")
    if keyboard is None:
        missing.append("keyboard")
    if pyperclip is None:
        missing.append("pyperclip")
    if missing:
        import tkinter as _tk
        from tkinter import messagebox as _mb
        root = _tk.Tk()
        root.withdraw()
        _mb.showerror(APP_TITLE,
                      f"缺少依赖库：{'、'.join(missing)}\n\n请先运行：pip install -r requirements.txt")
        return
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    App().mainloop()


if __name__ == "__main__":
    main()
