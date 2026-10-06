#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
EXIF 信息工具（合并版）
=======================
一个软件、两个功能模块（页面顶部标签切换，默认显示「EXIF 时间修复工具」）：

  ◆ EXIF 时间修复工具：按规则批量修复 EXIF 时间
    （有拍摄日期 → 统一为拍摄日期；无拍摄日期 → 用文件修改日期填充）
  ◆ EXIF 信息修改工具：自定义勾选写入/修改 14 项 EXIF 字段
    （制造商/型号/软件/拍摄日期/闪光灯/焦距/35mm焦距/快门/曝光偏差/
      光圈/ISO/曝光程序/测光模式/旋转信息）

共用能力：两种图片选择模式（路径自动扫描 / Windows 原生对话框手动多选）、
两种保存方式（backup 备份后原地修改 / changed 原图不动另存修改后）、
进度条、CSV 报告、backup\changed 目录自动跳过扫描、工具自身目录自动排除。

支持格式：JPG / JPEG / PNG / HEIC / HEIF（JPEG/PNG 无损，HEIC 重存 q95）。

用法：
  python exif_tool.py    # 启动网页界面（端口 8780 起，自动打开浏览器）
"""

import os
import sys
import csv
import io
import json
import shutil
import threading
import datetime as _dt
from fractions import Fraction

from PIL import Image
import piexif

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
    HAS_HEIF = True
except Exception:
    HAS_HEIF = False

# ---------------- 共享常量与工具 ----------------

SUPPORTED = {'.jpg': 'JPEG', '.jpeg': 'JPEG', '.png': 'PNG',
             '.heic': 'HEIF', '.heif': 'HEIF'}
JPEG_EXTS = {'.jpg', '.jpeg'}
PNG_EXTS = {'.png'}
HEIF_EXTS = {'.heic', '.heif'}

BACKUP_DIR_NAME = 'backup'
CHANGED_DIR_NAME = 'changed'
SKIP_DIRS = {BACKUP_DIR_NAME, CHANGED_DIR_NAME, '_exif_backup'}

EXIF_FMT = '%Y:%m:%d %H:%M:%S'
SHOW_FMT = '%Y-%m-%d %H:%M:%S'
TAG_OFFSET_TIMES = (36880, 36881, 36882)

ACT_NONE = '无需修改'
ACT_SYNC = '统一为拍摄日期'
ACT_FILL = '填充为修改日期'
ACT_ERR = '读取失败'


def self_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def show_dt(d):
    return d.strftime(SHOW_FMT) if d else ''


def empty_exif():
    return {'0th': {}, 'Exif': {}, 'GPS': {}, '1st': {}, 'Interop': {},
            'thumbnail': None}


def _exif_from_raw(raw):
    if not raw:
        return empty_exif()
    body = raw[6:] if raw[:6] == b'Exif\x00\x00' else raw
    try:
        return piexif.load(body)
    except Exception:
        return empty_exif()


def load_exif_dict(path, ext):
    if ext in JPEG_EXTS:
        with open(path, 'rb') as f:
            data = f.read()
        try:
            return piexif.load(data), None
        except Exception:
            return empty_exif(), None
    if ext in PNG_EXTS:
        with Image.open(path) as img:
            raw = img.info.get('exif')
        return _exif_from_raw(raw), None
    if ext in HEIF_EXTS:
        if not HAS_HEIF:
            return None, '未安装 pillow-heif，无法处理 HEIC/HEIF'
        with Image.open(path) as img:
            raw = img.info.get('exif')
        return _exif_from_raw(raw), None
    return None, '暂不支持该格式的写入'


def save_exif_bytes(path, ext, exif_bytes, dst=None):
    """把含新 EXIF 的字节写回图片。dst=None 原地修改；dst 给定则另存（原文件不动）。"""
    if dst is not None:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
    if ext in JPEG_EXTS:
        with open(path, 'rb') as f:
            data = f.read()
        tmp_clean = (dst or path) + '.exiftmp0'
        try:
            piexif.remove(data, tmp_clean)
            with open(tmp_clean, 'rb') as f:
                data = f.read()
        except Exception:
            pass
        finally:
            if os.path.exists(tmp_clean):
                os.remove(tmp_clean)
        if dst is None:
            tmp_out = path + '.exiftmp'
            piexif.insert(exif_bytes, data, tmp_out)
            os.replace(tmp_out, path)
        else:
            piexif.insert(exif_bytes, data, dst)
    elif ext in PNG_EXTS:
        with Image.open(path) as img:
            img.load()
            if dst is None:
                tmp = path + '.exiftmp'
                img.save(tmp, format='PNG', exif=exif_bytes)
                os.replace(tmp, path)
            else:
                img.save(dst, format='PNG', exif=exif_bytes)
    elif ext in HEIF_EXTS:
        with Image.open(path) as img:
            img.load()
            icc = img.info.get('icc_profile')
            kw = dict(format='HEIF', exif=exif_bytes, quality=95)
            if icc:
                kw['icc_profile'] = icc
            if dst is None:
                tmp = path + '.exiftmp'
                img.save(tmp, **kw)
                os.replace(tmp, path)
            else:
                img.save(dst, **kw)
    else:
        raise ValueError('暂不支持该格式的写入')


def _backup_file(path, backup_dir, src_dir=None):
    rel = os.path.relpath(path, src_dir) if src_dir else os.path.basename(path)
    dest = os.path.join(backup_dir, rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if not os.path.exists(dest):
        shutil.copy2(path, dest)


def sync_file_mtime(path, when):
    ts = when.timestamp()
    os.utime(path, (ts, ts))


def pick_images_dialog():
    """Windows 原生「打开文件」对话框（可多选）。取消返回 []。"""
    if os.name != 'nt':
        return []
    import ctypes
    from ctypes import wintypes

    class OPENFILENAMEW(ctypes.Structure):
        _fields_ = [
            ('lStructSize', wintypes.DWORD),
            ('hwndOwner', wintypes.HWND),
            ('hInstance', wintypes.HINSTANCE),
            ('lpstrFilter', ctypes.POINTER(wintypes.WCHAR)),
            ('lpstrCustomFilter', wintypes.LPWSTR),
            ('nMaxCustFilter', wintypes.DWORD),
            ('nFilterIndex', wintypes.DWORD),
            ('lpstrFile', ctypes.POINTER(wintypes.WCHAR)),
            ('nMaxFile', wintypes.DWORD),
            ('lpstrFileTitle', wintypes.LPWSTR),
            ('nMaxFileTitle', wintypes.DWORD),
            ('lpstrInitialDir', wintypes.LPCWSTR),
            ('lpstrTitle', wintypes.LPCWSTR),
            ('Flags', wintypes.DWORD),
            ('nFileOffset', wintypes.WORD),
            ('nFileExtension', wintypes.WORD),
            ('lpstrDefExt', wintypes.LPCWSTR),
            ('lCustData', wintypes.LPVOID),
            ('lpfnHook', wintypes.LPVOID),
            ('lpTemplateName', wintypes.LPCWSTR),
            ('pvReserved', ctypes.c_void_p),
            ('dwReserved', wintypes.DWORD),
            ('FlagsEx', wintypes.DWORD),
        ]

    filter_str = '图片文件\0*.jpg;*.jpeg;*.png;*.heic;*.heif\0所有文件\0*.*\0'
    filter_buf = ctypes.create_unicode_buffer(filter_str)
    file_buf = ctypes.create_unicode_buffer(65536)

    ofn = OPENFILENAMEW()
    ofn.lStructSize = ctypes.sizeof(OPENFILENAMEW)
    ofn.hwndOwner = None
    ofn.lpstrFilter = filter_buf
    ofn.lpstrFile = file_buf
    ofn.nMaxFile = len(file_buf)
    ofn.lpstrTitle = '选择图片（可按住 Ctrl 多选）'
    ofn.Flags = (0x200 | 0x80000 | 0x1000 | 0x4 | 0x8)

    if not ctypes.windll.comdlg32.GetOpenFileNameW(ctypes.byref(ofn)):
        return []
    raw = ctypes.wstring_at(ctypes.addressof(file_buf), ofn.nMaxFile)
    parts = [p for p in raw.split('\x00') if p]
    if not parts:
        return []
    if len(parts) == 1:
        return parts
    folder = parts[0]
    return [os.path.join(folder, n) for n in parts[1:]]


def guess_mime(n):
    l = n.lower()
    if l.endswith('.png'):
        return 'image/png'
    if l.endswith('.heic'):
        return 'image/heic'
    if l.endswith('.heif'):
        return 'image/heif'
    return 'image/jpeg'


# ================================================================
# 模块一：EXIF 时间修复工具（FF）
# ================================================================

FF_TAG_DATETIME = 306
FF_TAG_DATETIME_ORIGINAL = 36867
FF_TAG_DATETIME_DIGITIZED = 36868


def ff_parse_dt(value):
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode('utf-8', 'ignore')
    value = str(value).strip('\x00 ').strip()
    if not value:
        return None
    for fmt in (EXIF_FMT, '%Y-%m-%d %H:%M:%S'):
        try:
            return _dt.datetime.strptime(value, fmt)
        except ValueError:
            pass
    return None


def ff_read_times(exif_dict):
    shot = ff_parse_dt(exif_dict['Exif'].get(FF_TAG_DATETIME_ORIGINAL))
    exif_t = ff_parse_dt(exif_dict['0th'].get(FF_TAG_DATETIME))
    digit = ff_parse_dt(exif_dict['Exif'].get(FF_TAG_DATETIME_DIGITIZED))
    return shot, exif_t, digit


def ff_make_plan(path, ext):
    mtime_dt = _dt.datetime.fromtimestamp(os.path.getmtime(path))
    info = {'path': path, 'name': os.path.basename(path),
            'shot': None, 'exif_t': None, 'digit': None,
            'mtime': mtime_dt, 'new': None, 'action': '', 'note': ''}
    exif_dict, err = load_exif_dict(path, ext)
    if exif_dict is None:
        info['action'], info['note'] = ACT_ERR, (err or '无法读取')
        return info
    shot, exif_t, digit = ff_read_times(exif_dict)
    info['shot'], info['exif_t'], info['digit'] = shot, exif_t, digit
    if shot:
        info['new'] = shot
        if exif_t == shot and digit in (shot, None):
            info['action'] = ACT_NONE
        else:
            info['action'] = ACT_SYNC
    else:
        target = mtime_dt.replace(microsecond=0)
        info['new'] = target
        if exif_t == target and digit in (target, None):
            info['action'] = ACT_NONE
        else:
            info['action'] = ACT_FILL
    return info


def ff_stats_of(results, ignored=0):
    return {'total': len(results), 'ignored': ignored,
            'need_fix': sum(1 for r in results if r['action'] in (ACT_SYNC, ACT_FILL)),
            'ok': sum(1 for r in results if r['action'] == ACT_NONE),
            'err': sum(1 for r in results if r['action'] in (ACT_ERR,)),
            'has_shot': sum(1 for r in results if r['shot']),
            'no_shot': sum(1 for r in results if not r['shot'] and r['action'] != ACT_ERR)}


def ff_scan_folder(folder, recursive=True, ext_filter=None, exclude_dirs=None):
    ext_filter = ext_filter or set(SUPPORTED)
    exclude_abs = {os.path.abspath(d) for d in (exclude_dirs or []) if d}
    results = []
    ignored = 0

    def _handle(fp):
        nonlocal ignored
        ext = os.path.splitext(fp)[1].lower()
        if ext not in SUPPORTED or ext not in ext_filter:
            ignored += 1
            return
        rel_dir = os.path.dirname(os.path.relpath(fp, folder))
        try:
            item = ff_make_plan(fp, ext)
        except Exception as e:
            item = {'path': fp, 'name': os.path.basename(fp),
                    'shot': None, 'exif_t': None, 'digit': None,
                    'mtime': None, 'new': None,
                    'action': ACT_ERR, 'note': str(e)}
        item['rel_dir'] = rel_dir
        results.append(item)

    if recursive:
        for dirpath, dirnames, filenames in os.walk(folder):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            if exclude_abs:
                keep = []
                for d in dirnames:
                    full = os.path.abspath(os.path.join(dirpath, d))
                    if any(full == ex or full.startswith(ex + os.sep)
                           for ex in exclude_abs):
                        continue
                    keep.append(d)
                dirnames[:] = sorted(keep)
            for fn in sorted(filenames):
                _handle(os.path.join(dirpath, fn))
    else:
        for fn in sorted(os.listdir(folder)):
            fp = os.path.join(folder, fn)
            if os.path.isfile(fp):
                _handle(fp)

    return results, ff_stats_of(results, ignored)


def ff_apply_times_to_exif(exif_dict, when):
    s = when.strftime(EXIF_FMT).encode('ascii')
    exif_dict['0th'][FF_TAG_DATETIME] = s
    exif_dict['Exif'][FF_TAG_DATETIME_ORIGINAL] = s
    exif_dict['Exif'][FF_TAG_DATETIME_DIGITIZED] = s
    for tag in TAG_OFFSET_TIMES:
        exif_dict['Exif'].pop(tag, None)
    return exif_dict


def ff_apply_plans(items, folder, save_mode='inplace', backup=True,
                   sync_mtime=False, progress=None, backup_mode='root'):
    todo = [it for it in items if it['action'] in (ACT_SYNC, ACT_FILL)]
    done, failed = 0, 0
    for i, it in enumerate(todo):
        ext = os.path.splitext(it['path'])[1].lower()
        try:
            if save_mode == 'changed':
                src_dir = os.path.dirname(it['path'])
                if folder and folder in it['path']:
                    rel = os.path.relpath(it['path'], folder)
                    dst = os.path.join(folder, CHANGED_DIR_NAME, rel)
                else:
                    dst = os.path.join(src_dir, CHANGED_DIR_NAME, it['name'])
                exif_dict, err = load_exif_dict(it['path'], ext)
                if exif_dict is None:
                    raise ValueError(err or '无法读取')
                ff_apply_times_to_exif(exif_dict, it['new'])
                save_exif_bytes(it['path'], ext, piexif.dump(exif_dict), dst=dst)
                it['note'] = '已另存到 ' + CHANGED_DIR_NAME
            else:
                if backup:
                    if backup_mode == 'sibling':
                        src_dir = os.path.dirname(it['path'])
                        _backup_file(it['path'],
                                     os.path.join(src_dir, BACKUP_DIR_NAME), src_dir)
                    elif folder:
                        _backup_file(it['path'],
                                     os.path.join(folder, BACKUP_DIR_NAME), folder)
                exif_dict, err = load_exif_dict(it['path'], ext)
                if exif_dict is None:
                    raise ValueError(err or '无法读取')
                ff_apply_times_to_exif(exif_dict, it['new'])
                save_exif_bytes(it['path'], ext, piexif.dump(exif_dict))
                it['note'] = '完成'
                if sync_mtime and it['action'] == ACT_SYNC:
                    sync_file_mtime(it['path'], it['new'])
            done += 1
        except Exception as e:
            it['note'] = '失败: %s' % e
            failed += 1
        if progress:
            try:
                progress(i + 1, len(todo))
            except Exception:
                pass
    return done, failed


def ff_item_json(it):
    return {'name': it['name'], 'path': it['path'],
            'rel_dir': it.get('rel_dir', ''),
            'shot': show_dt(it.get('shot')), 'exif_t': show_dt(it.get('exif_t')),
            'mtime': show_dt(it.get('mtime')), 'new': show_dt(it.get('new')),
            'action': it['action'], 'note': it.get('note', '')}


# ================================================================
# 模块二：EXIF 信息修改工具（ED）
# ================================================================

ED_FIELD_DEFS = [
    {'key': 'Make',      'name': '相机制造商',  'ifd': '0th',  'tag': 0x010F, 'type': 'text', 'ph': 'Apple'},
    {'key': 'Model',     'name': '相机型号',    'ifd': '0th',  'tag': 0x0110, 'type': 'text', 'ph': 'iPad mini 2'},
    {'key': 'Software',  'name': '软件',        'ifd': '0th',  'tag': 0x0131, 'type': 'text', 'ph': '12.5.5'},
    {'key': 'DateTimeOriginal', 'name': '拍摄日期', 'ifd': 'Exif', 'tag': 36867,
     'type': 'datetime', 'ph': '2021/12/19 00:37:36'},
    {'key': 'Flash',     'name': '闪光灯',      'ifd': 'Exif', 'tag': 0x9209, 'type': 'enum',
     'options': [[0, '未闪光 (Flash did not fire)'], [1, '已闪光 (Flash fired)'],
                 [5, '闪光，返回光未检测'], [7, '闪光，返回光已检测'],
                 [9, '强制闪光 (compulsory flash)'], [16, '强制关闭闪光 (suppressed)'],
                 [24, '自动，未闪光'], [25, '自动，已闪光'],
                 [32, '无闪光功能 (No flash function)']]},
    {'key': 'FocalLength', 'name': '焦距 (mm)', 'ifd': 'Exif', 'tag': 0x920A,
     'type': 'rational', 'ph': '3.3'},
    {'key': 'FocalLengthIn35mmFilm', 'name': '焦距 (35mm)', 'ifd': 'Exif', 'tag': 0xA405,
     'type': 'int', 'ph': '32'},
    {'key': 'ExposureTime', 'name': '快门速度 (秒)', 'ifd': 'Exif', 'tag': 0x829A,
     'type': 'rational', 'ph': '1/25 或 0.04'},
    {'key': 'ExposureBiasValue', 'name': '曝光偏差 (EV)', 'ifd': 'Exif', 'tag': 0x9204,
     'type': 'srational', 'ph': '0.00 或 -1/3'},
    {'key': 'FNumber', 'name': '光圈数 (F 值)', 'ifd': 'Exif', 'tag': 0x829D,
     'type': 'fnumber', 'ph': 'f/2.4 或 2.4'},
    {'key': 'ISOSpeedRatings', 'name': 'ISO 感光度', 'ifd': 'Exif', 'tag': 0x8827,
     'type': 'int', 'ph': '320'},
    {'key': 'ExposureProgram', 'name': '曝光程序', 'ifd': 'Exif', 'tag': 0x8822, 'type': 'enum',
     'options': [[1, '手动 (Manual)'], [2, '标准程序 (Normal program)'],
                 [3, '光圈优先 (Aperture priority)'], [4, '快门优先 (Shutter priority)'],
                 [5, '创意程序 (Creative)'], [6, '行动程序 (Action)'],
                 [7, '人像模式 (Portrait)'], [8, '风景模式 (Landscape)'],
                 [9, 'B 快门 (Bulb)']]},
    {'key': 'MeteringMode', 'name': '测光模式', 'ifd': 'Exif', 'tag': 0x9207, 'type': 'enum',
     'options': [[0, '未知'], [1, '平均 (Average)'],
                 [2, '中心加权平均 (CenterWeightedAverage)'], [3, '点测光 (Spot)'],
                 [4, '多点测光 (MultiSpot)'], [5, '评估测光 (Pattern)'],
                 [6, '局部测光 (Partial)']]},
    {'key': 'Orientation', 'name': '旋转信息', 'ifd': '0th', 'tag': 0x0112, 'type': 'enum',
     'options': [[1, '正常 (Normal)'], [2, '水平翻转'], [3, '旋转 180°'],
                 [4, '垂直翻转'], [5, '横向翻转 + 旋转'], [6, '旋转 90° CW'],
                 [7, '旋转 270°'], [8, '旋转 90° CCW']]},
]

ED_FIELD_BY_KEY = {f['key']: f for f in ED_FIELD_DEFS}


def _ed_parse_text(s):
    s = s.strip()
    if not s:
        raise ValueError('内容为空')
    try:
        return s.encode('ascii')
    except UnicodeEncodeError:
        return s.encode('utf-8')


def _ed_parse_int(s):
    try:
        return int(s.strip())
    except ValueError:
        raise ValueError('应为整数，例如 320')


def _ed_parse_frac(s, signed=False):
    s = s.strip().lower()
    if s.startswith('f/'):
        s = s[2:]
    if '/' in s:
        a, b = s.split('/', 1)
        num, den = int(a.strip()), int(b.strip())
        if den == 0:
            raise ValueError('分母不能为 0')
        fr = Fraction(num, den)
    else:
        fr = Fraction(s)
    if fr < 0 and not signed:
        raise ValueError('不能为负数')
    fr = fr.limit_denominator(1000000)
    return (fr.numerator, fr.denominator)


def _ed_parse_dt(s):
    s = s.strip().replace('-', ':').replace('/', ':')
    if not s:
        raise ValueError('日期为空')
    for fmt in ('%Y:%m:%d %H:%M:%S', '%Y:%m:%d %H:%M'):
        try:
            return _dt.datetime.strptime(s, fmt).strftime(EXIF_FMT).encode('ascii')
        except ValueError:
            pass
    raise ValueError('日期格式应为 2021/12/19 00:37:36')


ED_FIELD_PARSERS = {
    'Make': _ed_parse_text, 'Model': _ed_parse_text, 'Software': _ed_parse_text,
    'DateTimeOriginal': _ed_parse_dt,
    'Flash': _ed_parse_int, 'FocalLengthIn35mmFilm': _ed_parse_int,
    'ISOSpeedRatings': _ed_parse_int, 'ExposureProgram': _ed_parse_int,
    'MeteringMode': _ed_parse_int, 'Orientation': _ed_parse_int,
    'FocalLength': lambda s: _ed_parse_frac(s),
    'ExposureTime': lambda s: _ed_parse_frac(s),
    'FNumber': lambda s: _ed_parse_frac(s),
    'ExposureBiasValue': lambda s: _ed_parse_frac(s, signed=True),
}


def ed_parse_fields(raw_fields):
    parsed = {}
    for key, raw in (raw_fields or {}).items():
        fdef = ED_FIELD_BY_KEY.get(key)
        if fdef is None:
            continue
        try:
            parsed[key] = ED_FIELD_PARSERS[key](str(raw))
        except ValueError as e:
            raise ValueError('字段「%s」的值无效：%s' % (fdef['name'], e))
        except Exception:
            raise ValueError('字段「%s」的值无效' % fdef['name'])
    return parsed


def ed_scan_folder(folder, recursive=True, exclude_dirs=None):
    exclude_abs = {os.path.abspath(d) for d in (exclude_dirs or []) if d}
    results = []
    ignored = 0

    def _handle(fp):
        nonlocal ignored
        ext = os.path.splitext(fp)[1].lower()
        if ext not in SUPPORTED:
            ignored += 1
            return
        rel_dir = os.path.dirname(os.path.relpath(fp, folder))
        results.append({'path': fp, 'name': os.path.basename(fp), 'rel_dir': rel_dir})

    if recursive:
        for dirpath, dirnames, filenames in os.walk(folder):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            if exclude_abs:
                keep = []
                for d in dirnames:
                    full = os.path.abspath(os.path.join(dirpath, d))
                    if any(full == ex or full.startswith(ex + os.sep)
                           for ex in exclude_abs):
                        continue
                    keep.append(d)
                dirnames[:] = sorted(keep)
            for fn in sorted(filenames):
                _handle(os.path.join(dirpath, fn))
    else:
        for fn in sorted(os.listdir(folder)):
            fp = os.path.join(folder, fn)
            if os.path.isfile(fp):
                _handle(fp)

    return results, {'total': len(results), 'ignored': ignored}


def ed_apply_items(items, folder, parsed, save_mode='inplace', progress=None):
    done, failed = 0, 0
    total = len(items)
    for i, it in enumerate(items):
        ext = os.path.splitext(it['path'])[1].lower()
        try:
            if save_mode == 'changed':
                src_dir = os.path.dirname(it['path'])
                if folder and folder in it['path']:
                    rel = os.path.relpath(it['path'], folder)
                    dst = os.path.join(folder, CHANGED_DIR_NAME, rel)
                else:
                    dst = os.path.join(src_dir, CHANGED_DIR_NAME, it['name'])
                exif_dict, err = load_exif_dict(it['path'], ext)
                if exif_dict is None:
                    raise ValueError(err or '无法读取')
                _ed_set_fields(exif_dict, parsed)
                save_exif_bytes(it['path'], ext, piexif.dump(exif_dict), dst=dst)
                it['note'] = '已另存到 ' + CHANGED_DIR_NAME
            else:
                src_dir = os.path.dirname(it['path'])
                backup_dir = (os.path.join(folder, BACKUP_DIR_NAME)
                              if folder and folder in it['path']
                              else os.path.join(src_dir, BACKUP_DIR_NAME))
                _backup_file(it['path'], backup_dir)
                exif_dict, err = load_exif_dict(it['path'], ext)
                if exif_dict is None:
                    raise ValueError(err or '无法读取')
                _ed_set_fields(exif_dict, parsed)
                save_exif_bytes(it['path'], ext, piexif.dump(exif_dict))
                it['note'] = '完成（写入 %d 项）' % len(parsed)
            done += 1
        except Exception as e:
            it['note'] = '失败: %s' % e
            failed += 1
        if progress:
            try:
                progress(i + 1, total)
            except Exception:
                pass
    return done, failed


def _ed_set_fields(exif_dict, parsed):
    zeroth, exif = exif_dict['0th'], exif_dict['Exif']
    for key, val in parsed.items():
        fdef = ED_FIELD_BY_KEY[key]
        if key == 'DateTimeOriginal':
            zeroth[306] = val
            exif[36867] = val
            exif[36868] = val
            for t in TAG_OFFSET_TIMES:
                exif.pop(t, None)
        else:
            target = zeroth if fdef['ifd'] == '0th' else exif
            target[fdef['tag']] = val


def ed_item_json(it, summary):
    return {'name': it['name'], 'path': it['path'],
            'rel_dir': it.get('rel_dir', ''),
            'summary': summary, 'note': it.get('note', '')}


# ================================================================
# 网页界面（双 Tab）
# ================================================================

PAGE = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>EXIF 信息工具</title>
<style>
:root{
  --bg:#f4f5f7; --card:#ffffff; --ink:#1f2430; --ink2:#5b6472; --line:#e3e6ec;
  --accent:#2563eb; --accent2:#0e9f6e; --accent-soft:#e8efff; --chip:#eef1f5; --err:#cc2936;
}
@media (prefers-color-scheme: dark){
  :root{ --bg:#15181e; --card:#1e222a; --ink:#e8eaf0; --ink2:#9aa3b2; --line:#2c313c;
    --accent:#5b93ff; --accent2:#34d399; --accent-soft:#1d2a45; --chip:#262b35; --err:#ff6b6b; }
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:14px/1.6 "Segoe UI","Microsoft YaHei",system-ui,sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:18px 16px 60px}
h1{font-size:21px;margin:0 0 10px}
.tabs{display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap}
.tabs button{border:1px solid var(--line);background:var(--card);color:var(--ink);
  border-radius:10px;padding:9px 20px;font-size:14px;font-weight:600;cursor:pointer}
.tabs button.active{background:var(--accent);color:#fff;border-color:var(--accent)}
.tabsec{display:none}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
  padding:14px 16px;margin-bottom:14px}
.rule{font-size:12.5px;color:var(--ink2)}
.rule b{color:var(--ink)}
.row{display:flex;flex-wrap:wrap;gap:10px;align-items:center}
input[type=text],select{padding:7px 9px;border:1px solid var(--line);
  border-radius:8px;background:var(--bg);color:var(--ink);font-size:13px}
#ff_folder,#ed_folder{flex:1;min-width:280px}
label.opt{display:inline-flex;align-items:center;gap:5px;font-size:13px;
  color:var(--ink2);cursor:pointer;user-select:none;line-height:1.4}
label.opt input[type=checkbox],label.opt input[type=radio]{
  width:14px;height:14px;margin:0;flex:none;vertical-align:middle}
td.selcell{display:flex;align-items:center;justify-content:center;gap:7px;
  white-space:nowrap}
button{border:none;border-radius:8px;padding:9px 18px;font-size:13.5px;cursor:pointer;
  background:var(--accent);color:#fff;font-weight:600}
button:hover{filter:brightness(1.08)}
button:disabled{opacity:.45;cursor:not-allowed}
button.ghost{background:var(--chip);color:var(--ink);font-weight:500}
button.danger{background:var(--err)}
#ed_summary{font-size:13px;color:var(--ink);word-break:break-all}
.tblwrap{overflow:auto;max-height:420px;border:1px solid var(--line);border-radius:10px}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{position:sticky;top:0;background:var(--chip);text-align:left;padding:7px 10px;
  color:var(--ink2);font-weight:600;white-space:nowrap;z-index:1}
th.sortable{cursor:pointer;user-select:none}
.delrow{background:none;border:none;color:var(--err);cursor:pointer;font-weight:700;
  font-size:14px;line-height:1;padding:0 3px;border-radius:4px;vertical-align:middle}
.delrow:hover{color:#fff;background:var(--err)}
th .delrow{font-size:15px;padding:1px 6px}
th.sortable:hover{color:var(--ink)}
.arrows{font-size:10px;color:var(--accent);margin-left:2px}
td{padding:6px 10px;border-top:1px solid var(--line);white-space:nowrap}
tr.a-none td{color:var(--ink2)}
tr.a-sync td.act{color:var(--accent);font-weight:600}
tr.a-fill td.act{color:var(--c26a00,#c26a00);font-weight:600}
tr.a-err td{color:var(--err)}
tr.ok td{color:var(--ink)}
tr.fail td{color:var(--err)}
.empty{padding:26px;text-align:center;color:var(--ink2);font-size:13px}
#ff_log,#ed_log{font:12px/1.7 Consolas,monospace;color:var(--ink2);white-space:pre-wrap;
  max-height:130px;overflow:auto}
.foot{margin-top:14px;font-size:12px;color:var(--ink2)}
.fldgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:6px 18px}
.fldrow{display:flex;gap:8px;align-items:center;padding:2px 0}
.fldname{width:130px;font-size:13px;flex:none}
.fldrow input[type=text],.fldrow select{flex:1;min-width:0}
.fldrow input[type=checkbox]{flex:none}
.fldrow.off input[type=text],.fldrow.off select{opacity:.4}
.statsrow{display:flex;flex-wrap:wrap;gap:8px;margin:2px 0 10px}
.chip{background:var(--chip);border-radius:999px;padding:3px 12px;font-size:12.5px;color:var(--ink2)}
.chip b{color:var(--ink)}
</style>
</head>
<body>
<div class="wrap">
  <div style="display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin-bottom:10px">
    <h1 style="margin:0">EXIF 信息工具</h1>
    <span style="font-size:12.5px;color:var(--ink2)"><a href="https://space.bilibili.com/702444737?spm_id_from=333.1007.0.0"
         target="_blank" rel="noopener"
         style="color:#fb7299;font-weight:600;text-decoration:none">bilibili@何止有点帅</a> · 如果软件有帮到你，希望对 up 的软件表达感谢和支持，可以
      <a href="https://www.bilibili.com/video/BV1p1daBMErQ/?spm_id_from=333.1387.homepage.video_card.click&amp;vd_source=40169949e69893de442bdc5d692f88e5"
         target="_blank" rel="noopener"
         style="color:var(--accent);font-weight:600;text-decoration:none;border-bottom:1px dashed var(--accent);padding-bottom:1px">为 up 的另一个视频增加播放量支持 →</a>
      ，让 up 领取播放量奖励，感谢各位！</span>
  </div>
  <div class="tabs">
    <button id="tabbtn_ff" class="active">EXIF 时间修复工具</button>
    <button id="tabbtn_ed">EXIF 信息修改工具</button>
  </div>

  <!-- ============ Tab 1：EXIF 时间修复工具 ============ -->
  <div id="tab_ff" class="tabsec">
    <div class="card rule">
      <b>修复规则：</b>① 有拍摄日期的照片 → EXIF 的修改时间/数字化时间统一为<b>拍摄日期</b>；
      ② 没有拍摄日期的照片（截图等）→ 用<b>文件修改日期</b>填充 EXIF 时间。
    </div>
    <div class="card">
      <div class="row" style="margin-bottom:10px">
        <input type="text" id="ff_folder" value="__DEFAULT_FOLDER__" spellcheck="false"
               placeholder="输入照片文件夹路径，例如 C:\Users\me\Pictures">
        <button class="ghost" id="ff_btnScan">扫描预览</button>
        <button class="ghost" id="ff_btnPick" title="弹出 Windows 文件选择对话框，可跨文件夹多选">手动选择图片…</button>
      </div>
      <div class="row">
        <label class="opt"><input type="checkbox" id="ff_recursive">包含子文件夹</label>
        <label class="opt"><input type="checkbox" id="ff_jpg" checked>JPG</label>
        <label class="opt"><input type="checkbox" id="ff_png" checked>PNG</label>
        <label class="opt"><input type="checkbox" id="ff_heic" __FF_HEIC__>HEIC/HEIF__FF_HEIC_HINT__</label>
        <label class="opt" id="ff_syncWrap"><input type="checkbox" id="ff_syncmtime">文件修改时间同步为拍摄日期</label>
      </div>
      <div class="row" style="margin-top:8px">
        <span style="font-size:13px;color:var(--ink2)">保存方式：</span>
        <label class="opt"><input type="radio" name="ff_savemode" value="inplace">备份原图片到 backup\ 并原地修改</label>
        <label class="opt"><input type="radio" name="ff_savemode" value="changed" checked>原图片不动，修改后另存到 changed\</label>
      </div>
    </div>
    <div class="card">
      <div class="statsrow" id="ff_stats"></div>
      <div class="row" style="margin-bottom:10px">
        <button id="ff_btnApply" onclick="ffAskApply()" disabled>应用修改</button>
        <button class="ghost" id="ff_btnCsv" onclick="ffExportCsv()" disabled>导出报告 CSV</button>
        <button class="danger ghost" id="ff_btnShutdown">退出工具</button>
        <span id="ff_msg" style="font-size:13px;color:var(--ink2)"></span>
      </div>
      <div id="ff_confirmBox" style="display:none;background:var(--accent-soft);border:1px solid var(--accent);border-radius:10px;padding:10px 14px;margin-bottom:10px">
        <span id="ff_cfText" style="font-size:13px"></span>
        <div class="row" style="margin-top:8px">
          <button id="ff_cfYes">确认修改</button>
          <button class="ghost" id="ff_cfNo">取消</button>
        </div>
      </div>
      <div id="ff_pgBox" style="display:none;background:var(--accent-soft);border:1px solid var(--accent);border-radius:10px;padding:12px 16px;margin-bottom:10px">
        <div style="display:flex;justify-content:space-between;font-size:14px;font-weight:600;margin-bottom:8px">
          <span id="ff_pgPhase">正在准备…</span>
          <span id="ff_pgNum" style="color:var(--ink2);font-weight:500">0 / 0</span>
        </div>
        <div style="height:12px;background:var(--chip);border-radius:999px;overflow:hidden">
          <div id="ff_pgBar" style="height:100%;width:0%;background:var(--accent);border-radius:999px;transition:width .3s"></div>
        </div>
      </div>
      <div class="tblwrap">
        <table id="ff_table">
          <thead><tr>
            <th style="width:72px;text-align:center;white-space:nowrap"><input type="checkbox" id="ff_selAll" title="全选/全不选"><button class="delrow" id="ff_clearAll" title="清空全部清单">×</button></th>
            <th class="sortable" data-key="name">文件名<span id="ff_ar_name" class="arrows"></span></th>
            <th class="sortable" data-key="rel_dir">所在文件夹<span id="ff_ar_rel_dir" class="arrows"></span></th>
            <th class="sortable" data-key="shot">拍摄日期<span id="ff_ar_shot" class="arrows"></span></th>
            <th class="sortable" data-key="exif_t">EXIF 时间<span id="ff_ar_exif_t" class="arrows"></span></th>
            <th class="sortable" data-key="mtime">文件修改时间<span id="ff_ar_mtime" class="arrows"></span></th>
            <th class="sortable" data-key="new">将写入的时间<span id="ff_ar_new" class="arrows"></span></th>
            <th>操作</th><th>备注</th>
          </tr></thead>
          <tbody id="ff_rows"><tr><td colspan="9" class="empty">点「扫描预览」自动扫描文件夹，或点「手动选择图片…」挑选图片——查看将要执行的修改（此步不改动任何文件）</td></tr></tbody>
        </table>
      </div>
    </div>
    <div class="card"><div id="ff_log">日志就绪。</div></div>
    <div class="foot">保存方式说明：<b>backup 方式</b>会把原图片备份到 backup\ 文件夹后原地修改（模式一统一放所选文件夹下并保持子目录结构，模式二放各图片所在目录）；<b>changed 方式</b>原图片保持不动，修改后的图片另存到 changed\ 文件夹。
    勾选「文件修改时间同步为拍摄日期」后，已有拍摄日期的照片其文件修改时间也会改为拍摄时间（仅 backup 方式生效）。</div>
  </div>

  <!-- ============ Tab 2：EXIF 信息修改工具 ============ -->
  <div id="tab_ed" class="tabsec" style="display:none">
    <div class="card">
      <div class="row" style="margin-bottom:10px">
        <input type="text" id="ed_folder" value="__DEFAULT_FOLDER__" spellcheck="false"
               placeholder="输入照片文件夹路径，例如 C:\Users\me\Pictures">
        <button class="ghost" id="ed_btnScan">扫描预览</button>
        <button class="ghost" id="ed_btnPick" title="弹出 Windows 文件选择对话框，可跨文件夹多选">手动选择图片…</button>
      </div>
      <div class="row">
        <label class="opt"><input type="checkbox" id="ed_recursive">包含子文件夹</label>
        <span style="font-size:13px;color:var(--ink2)">保存方式：</span>
        <label class="opt"><input type="radio" name="ed_savemode" value="inplace">备份原图片到 backup\ 并原地修改</label>
        <label class="opt"><input type="radio" name="ed_savemode" value="changed" checked>原图片不动，修改后另存到 changed\</label>
      </div>
    </div>
    <div class="card">
      <div style="font-size:14px;font-weight:600;margin-bottom:8px">EXIF 字段（勾选要写入的项，可单项或多项）</div>
      <div class="fldgrid" id="ed_fieldsBox"></div>
      <div style="margin-top:10px;font-size:12.5px;color:var(--ink2)">当前将写入：</div>
      <div id="ed_summary">（未选择任何字段）</div>
    </div>
    <div class="card">
      <div class="row" style="margin-bottom:10px">
        <span id="ed_msg" style="font-size:13px;color:var(--ink2)">先勾选字段、再选择图片</span>
        <span id="ed_selCount" style="font-size:13px;color:var(--ink2);margin-left:auto"></span>
      </div>
      <div class="row" style="margin-bottom:10px">
        <button id="ed_btnApply" disabled>应用修改</button>
        <button class="ghost" id="ed_btnCsv" disabled>导出报告 CSV</button>
        <button class="danger ghost" id="ed_btnShutdown">退出工具</button>
      </div>
      <div id="ed_confirmBox" style="display:none;background:var(--accent-soft);border:1px solid var(--accent2);border-radius:10px;padding:10px 14px;margin-bottom:10px">
        <span id="ed_cfText" style="font-size:13px"></span>
        <div class="row" style="margin-top:8px">
          <button id="ed_cfYes">确认修改</button>
          <button class="ghost" id="ed_cfNo">取消</button>
        </div>
      </div>
      <div id="ed_pgBox" style="display:none;background:var(--accent-soft);border:1px solid var(--accent2);border-radius:10px;padding:12px 16px;margin-bottom:10px">
        <div style="display:flex;justify-content:space-between;font-size:14px;font-weight:600;margin-bottom:8px">
          <span id="ed_pgPhase">正在准备…</span>
          <span id="ed_pgNum" style="color:var(--ink2);font-weight:500">0 / 0</span>
        </div>
        <div style="height:12px;background:var(--chip);border-radius:999px;overflow:hidden">
          <div id="ed_pgBar" style="height:100%;width:0%;background:var(--accent2);border-radius:999px;transition:width .3s"></div>
        </div>
      </div>
      <div class="tblwrap">
        <table id="ed_table">
          <thead><tr>
            <th style="width:72px;text-align:center;white-space:nowrap"><input type="checkbox" id="ed_selAll" title="全选/全不选"><button class="delrow" id="ed_clearAll" title="清空全部清单">×</button></th>
            <th>文件名</th><th>所在文件夹</th><th>将写入的内容</th><th>状态</th>
          </tr></thead>
          <tbody id="ed_rows"><tr><td colspan="5" class="empty">点「扫描预览」或「手动选择图片」列出目标文件</td></tr></tbody>
        </table>
      </div>
    </div>
    <div class="card"><div id="ed_log">日志就绪。</div></div>
    <div class="foot">日期写入时会同时更新 EXIF 的 DateTime / DateTimeOriginal / DateTimeDigitized 三项。
    旋转信息 (Orientation) 只改 EXIF 标记不改像素，看图软件会按标记旋转显示。</div>
  </div>
</div>
<script>
function switchTab(t){
 document.getElementById('tab_ff').style.display=t==='ff'?'block':'none';
 document.getElementById('tab_ed').style.display=t==='ed'?'block':'none';
 document.getElementById('tabbtn_ff').classList.toggle('active',t==='ff');
 document.getElementById('tabbtn_ed').classList.toggle('active',t==='ed');
}
document.getElementById('tabbtn_ff').addEventListener('click',()=>switchTab('ff'));
document.getElementById('tabbtn_ed').addEventListener('click',()=>switchTab('ed'));
</script>
<script>
/* ============ Tab 1：EXIF 时间修复工具 ============ */
(function(){
const $=id=>document.getElementById(id);
let curItems=[],sortKey='',sortDir=1,pickMode=false,pickList=[],pgTimer=null,lastNeed=0;
const SORT_KEYS=['name','rel_dir','shot','exif_t','mtime','new'];
const API='/api/ff';
function log(s){const d=new Date().toLocaleTimeString();$('ff_log').textContent+='['+d+'] '+s+'\n';$('ff_log').scrollTop=1e9;}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function saveMode(){const e=document.querySelector('input[name=ff_savemode]:checked');return e?e.value:'inplace';}
function syncUi(){const ch=saveMode()==='changed';
 $('ff_syncmtime').disabled=ch;
 if(ch)$('ff_syncmtime').checked=false;}
document.querySelectorAll('input[name=ff_savemode]').forEach(r=>r.addEventListener('change',syncUi));
syncUi();
async function post(url,body,timeout){const c=new AbortController();const t=setTimeout(()=>c.abort(),timeout||120000);
 try{const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},
 body:JSON.stringify(body),signal:c.signal});
 return await r.json();}
 finally{clearTimeout(t);}}
function opts(){return{folder:$('ff_folder').value.trim(),recursive:$('ff_recursive').checked,
 jpg:$('ff_jpg').checked,png:$('ff_png').checked,heic:$('ff_heic').checked,
 save_mode:saveMode()};}
function chips(st){if(!st)return;$('ff_stats').innerHTML=
 `<span class="chip">共 <b>${st.total}</b> 张</span>`+
 `<span class="chip">有拍摄日期 <b>${st.has_shot}</b></span>`+
 `<span class="chip">无拍摄日期 <b>${st.no_shot}</b></span>`+
 `<span class="chip" style="color:var(--accent)">待修改 <b>${st.need_fix}</b></span>`+
 `<span class="chip">已一致 <b>${st.ok}</b></span>`+
 (st.err?`<span class="chip" style="color:var(--err)">异常 <b>${st.err}</b></span>`:'')+
 `<span class="chip">忽略其他文件 <b>${st.ignored}</b></span>`;}
function render(items){curItems=items;
 if(!items.length){selSet=new Set();updateNeed();
  $('ff_rows').innerHTML='<tr><td colspan="9" class="empty">未找到符合条件的照片</td></tr>';return;}
 const cls={'无需修改':'a-none','统一为拍摄日期':'a-sync','填充为修改日期':'a-fill'};
 const known=items.every(it=>selSet.has(it.path));
 if(!known)selSet=new Set(items.map(it=>it.path));
 $('ff_rows').innerHTML=items.map(it=>{
  const c=cls[it.action]||'a-err';
  return `<tr class="${c}"><td class="selcell"><input type="checkbox" class="ff_sel" data-path="${esc(it.path)}" ${selSet.has(it.path)?'checked':''}><button class="delrow" data-path="${esc(it.path)}" title="从清单中移除">×</button></td>`+
   `<td>${esc(it.name)}</td><td>${esc(it.rel_dir||'—')}</td><td>${it.shot||'—'}</td><td>${it.exif_t||'—'}</td>`+
   `<td>${it.mtime||'—'}</td><td>${it.new||'—'}</td><td class="act">${it.action}</td><td>${esc(it.note||'')}</td></tr>`;
 }).join('');
 document.querySelectorAll('#ff_rows input.ff_sel').forEach(cb=>
  cb.addEventListener('change',()=>{const p=cb.dataset.path;
   if(cb.checked)selSet.add(p);else selSet.delete(p);updateNeed();}));
 document.querySelectorAll('#ff_rows button.delrow').forEach(b=>
  b.addEventListener('click',()=>delRow(b.dataset.path)));
 updateNeed();}
let selSet=new Set();
function updateNeed(){lastNeed=0;
 curItems.forEach(it=>{if(selSet.has(it.path)&&(it.action==='统一为拍摄日期'||it.action==='填充为修改日期'))lastNeed++;});
 const sa=$('ff_selAll');
 if(sa){const total=curItems.length;
  const selN=curItems.filter(it=>selSet.has(it.path)).length;
  sa.checked=total>0&&selN===total;
  sa.indeterminate=selN>0&&selN<total;}
 $('ff_btnApply').disabled=lastNeed===0;}
function sortBy(k){if(sortKey===k)sortDir*=-1;else{sortKey=k;sortDir=1;}
 SORT_KEYS.forEach(x=>{const e=$('ff_ar_'+x);if(e)e.textContent=x===sortKey?(sortDir>0?'▲':'▼'):'';});
 renderSorted();}
function renderSorted(){if(!sortKey)return;
 const arr=curItems.slice();
 arr.sort((a,b)=>{const va=(a[sortKey]||'').toString(),vb=(b[sortKey]||'').toString();
  if(!va&&!vb)return 0;if(!va)return 1;if(!vb)return -1;
  return va.localeCompare(vb,'zh-CN')*sortDir;});
 render(arr);}
async function scan(){if(pgTimer){alert('正在修改中，请等待完成');return;}
 $('ff_msg').textContent='扫描中，请稍候…';
 try{const j=await post(API+'/scan',opts());
  if(!j.ok){$('ff_msg').textContent='扫描失败: '+j.error;log('扫描失败: '+j.error);return;}
  chips(j.stats);render(j.items);
  $('ff_btnApply').disabled=!j.stats.need_fix;$('ff_btnCsv').disabled=false;
  pickMode=false;pickList=[];
  $('ff_msg').textContent='扫描完成';log(`扫描完成: 共 ${j.stats.total} 张，待修改 ${j.stats.need_fix} 张`);
 }catch(e){$('ff_msg').textContent='请求失败: '+e;
 alert('请求失败: '+e+'\n\n若反复失败：请重新启动工具，再用浏览器打开页面地址');}}
let pickBusy=false;
async function pickFiles(){if(pgTimer){alert('正在修改中，请等待完成');return;}
 if(pickBusy)return;pickBusy=true;
 $('ff_msg').textContent='已弹出文件选择对话框：请在其中选图（Ctrl 多选），选好后回到本页面';
 try{const j=await fetch('/api/pickfiles',{method:'POST',
  headers:{'Content-Type':'application/json'},body:'{}'});
  const g=await j.json();
  if(!g.ok){$('ff_msg').textContent='打开文件选择器失败: '+(g.error||'');return;}
  if(!g.files.length){$('ff_msg').textContent='未选择任何图片';log('手动选择: 已取消');return;}
  const known=new Set(curItems.map(it=>it.path));
  const added=g.files.filter(p=>!known.has(p));
  if(!added.length){$('ff_msg').textContent='所选图片都已在清单中（共 '+curItems.length+' 张）';
   log('手动选择: 新增 0 张（全部重复），清单共 '+curItems.length+' 张');return;}
  added.forEach(p=>selSet.add(p));
  pickList=curItems.map(it=>it.path).concat(added);
  pickMode=true;
  const j2=await post(API+'/analyze_files',{files:pickList});
  if(!j2.ok){$('ff_msg').textContent='分析失败: '+j2.error;return;}
  chips(j2.stats);render(j2.items);
  $('ff_btnApply').disabled=!j2.stats.need_fix;$('ff_btnCsv').disabled=false;
  $('ff_msg').textContent='手动选择完成: 新增 '+added.length+' 张，清单共 '+j2.stats.total+' 张，待修改 '+j2.stats.need_fix+' 张';
  log($('ff_msg').textContent);
 }catch(e){$('ff_msg').textContent='请求失败: '+e;}
 finally{pickBusy=false;}}
function setPg(text,done,total){$('ff_pgPhase').textContent=text;
 $('ff_pgNum').textContent=done+' / '+total;
 $('ff_pgBar').style.width=total>0?Math.round(done/total*100)+'%':'0%';}
function askApply(){if(!lastNeed){$('ff_msg').textContent='没有待修改的照片，请先扫描预览或手动选择图片';return;}
 const sm=saveMode();
 let where=sm==='changed'
  ?(pickMode?'修改后的图片会另存到各图片所在目录的 changed\\ 内，原图片保持不动。'
            :'修改后的图片会另存到 <所选文件夹>\\changed\\（保持子目录结构），原图片保持不动。')
  :(pickMode?'原图片会先备份到各自所在目录的 backup\\ 内，然后原地修改。'
            :'原图片会先备份到 <所选文件夹>\\backup\\ ，然后原地修改。');
 $('ff_cfText').textContent='即将处理 '+lastNeed+' 张照片（JPEG/PNG 无损，HEIC 重存 q95）。'+where+
  (sm==='inplace'&&$('ff_syncmtime').checked?' 文件修改时间将同步为拍摄日期。':'');
 $('ff_confirmBox').style.display='block';}
function cancelApply(){$('ff_confirmBox').style.display='none';}
async function doApply(){if(!pickMode)setPg('准备中：重新扫描照片…',0,0);else setPg('准备中：分析所选图片…',0,0);
 $('ff_confirmBox').style.display='none';
 const b=$('ff_btnApply');b.disabled=true;b.textContent='正在修改…';
 $('ff_btnScan').disabled=true;$('ff_btnPick').disabled=true;
 $('ff_pgBox').style.display='block';
 $('ff_msg').textContent='正在修改…';
 const modeBody=pickMode?{mode:'files',files:curItems.map(it=>it.path)}:{};
 modeBody.selected=Array.from(selSet);
 pgTimer=setInterval(async()=>{try{const r=await fetch(API+'/progress');const g=await r.json();
  if(!g.applying)return;
  if(g.phase==='writing')setPg('正在写入 EXIF 时间',g.done,g.total);
  else if(g.phase==='rescanning')setPg('写入完成，正在重新核对…',g.total,g.total);
  else setPg(pickMode?'准备中：分析所选图片…':'准备中：重新扫描照片…',0,0);
  }catch(e){}},500);
 try{const j=await post(API+'/apply',Object.assign(opts(),
   {backup:saveMode()==='inplace',
    sync_mtime:saveMode()==='inplace'&&$('ff_syncmtime').checked},modeBody));
  if(!j.ok){$('ff_msg').textContent='失败: '+j.error;log('失败: '+j.error);
   setPg('失败: '+j.error,0,0);$('ff_pgBar').style.background='var(--err)';}
  else{chips(j.stats);render(j.items);
   $('ff_msg').textContent='修改完成: 成功 '+j.done+' 张'+(j.failed?', 失败 '+j.failed+' 张':'');
   setPg('✓ 修改完成: 成功 '+j.done+' 张'+(j.failed?', 失败 '+j.failed+' 张':''),j.done,Math.max(j.done,1));
   log($('ff_msg').textContent);
   if(saveMode()==='changed')log('原图片保持不变；修改后的图片已另存到 changed\\ 文件夹。');
   else log('照片已原地更新（仍在原文件夹）；原图片备份在 backup\\ 文件夹内。');}
 }catch(e){$('ff_msg').textContent='请求失败: '+e;
 setPg('请求失败，请重试',0,0);$('ff_pgBar').style.background='var(--err)';}
 finally{clearInterval(pgTimer);pgTimer=null;b.textContent='应用修改';
 b.disabled=lastNeed===0;$('ff_btnScan').disabled=false;$('ff_btnPick').disabled=false;
 setTimeout(()=>{$('ff_pgBox').style.display='none';$('ff_pgBar').style.background='var(--accent)';},8000);}}
function shutdown(){if(!confirm('退出工具？'))return;
 fetch(API+'/shutdown',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}).catch(()=>{});
 document.body.innerHTML='<div class="wrap"><h1>已退出</h1><div class="sub">可以关闭此页面了。</div></div>';}
$('ff_btnScan').addEventListener('click',scan);
$('ff_btnPick').addEventListener('click',pickFiles);
$('ff_btnApply').addEventListener('click',askApply);
$('ff_btnCsv').addEventListener('click',()=>{location.href=API+'/report';});
$('ff_btnShutdown').addEventListener('click',shutdown);
$('ff_cfYes').addEventListener('click',doApply);
$('ff_cfNo').addEventListener('click',cancelApply);
document.querySelectorAll('#ff_table th.sortable').forEach(th=>th.addEventListener('click',()=>sortBy(th.dataset.key)));
$('ff_selAll').addEventListener('change',()=>{
 const on=$('ff_selAll').checked;
 curItems.forEach(it=>{if(on)selSet.add(it.path);else selSet.delete(it.path);});
 document.querySelectorAll('#ff_rows input.ff_sel').forEach(cb=>{cb.checked=on;});
 updateNeed();});
function ffRecount(){chips({total:curItems.length,
 has_shot:curItems.filter(it=>it.shot).length,
 no_shot:curItems.filter(it=>!it.shot&&it.action!=='读取失败').length,
 need_fix:curItems.filter(it=>it.action==='统一为拍摄日期'||it.action==='填充为修改日期').length,
 ok:curItems.filter(it=>it.action==='无需修改').length,
 err:curItems.filter(it=>it.action==='读取失败').length,ignored:0});}
function delRow(p){curItems=curItems.filter(it=>it.path!==p);selSet.delete(p);
 if(pickMode)pickList=curItems.map(it=>it.path);
 render(curItems);ffRecount();updateNeed();
 $('ff_msg').textContent='已从清单移除 1 张，剩 '+curItems.length+' 张';}
function clearAllFf(){if(!curItems.length)return;
 if(!confirm('清空全部清单？'))return;
 curItems=[];selSet.clear();pickMode=false;pickList=[];
 render([]);ffRecount();updateNeed();
 $('ff_msg').textContent='清单已清空';log('清单已清空');}
document.querySelectorAll('#ff_rows button.delrow').forEach(b=>
 b.addEventListener('click',()=>delRow(b.dataset.path)));
$('ff_clearAll').addEventListener('click',clearAllFf);
})();
</script>
<script>
/* ============ Tab 2：EXIF 信息修改工具 ============ */
(function(){
const $=id=>document.getElementById(id);
const FIELDS=__FIELDS_JSON__;
let curItems=[],pickMode=false,pickList=[],pgTimer=null,lastCount=0;
const API='/api/ed';
function log(s){const d=new Date().toLocaleTimeString();$('ed_log').textContent+='['+d+'] '+s+'\n';$('ed_log').scrollTop=1e9;}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function saveMode(){const e=document.querySelector('input[name=ed_savemode]:checked');return e?e.value:'inplace';}
async function post(url,body,timeout){const c=new AbortController();const t=setTimeout(()=>c.abort(),timeout||600000);
 try{const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},
 body:JSON.stringify(body),signal:c.signal});
 return await r.json();}
 finally{clearTimeout(t);}}
FIELDS.forEach(f=>{
 const row=document.createElement('div');row.className='fldrow off';
 const cb=document.createElement('input');cb.type='checkbox';cb.id='ed_en_'+f.key;
 const sp=document.createElement('span');sp.className='fldname';sp.textContent=f.name;
 let inp;
 if(f.options){inp=document.createElement('select');inp.id='ed_fld_'+f.key;inp.disabled=true;
  const o0=document.createElement('option');o0.value='';o0.textContent='（选择）';
  inp.appendChild(o0);
  f.options.forEach(op=>{const o=document.createElement('option');o.value=op[0];o.textContent=op[1];inp.appendChild(o);});
 }else{inp=document.createElement('input');inp.type='text';inp.id='ed_fld_'+f.key;inp.placeholder=f.ph||'';inp.disabled=true;}
 cb.addEventListener('change',()=>{row.classList.toggle('off',!cb.checked);inp.disabled=!cb.checked;
  if(!cb.checked)inp.value='';updateSummary();});
 inp.addEventListener('input',updateSummary);inp.addEventListener('change',updateSummary);
 row.append(cb,sp,inp);$('ed_fieldsBox').append(row);});
function collectFields(){const out={};
 FIELDS.forEach(f=>{const cb=$('ed_en_'+f.key);if(!cb.checked)return;
  const v=$('ed_fld_'+f.key).value.trim();
  if(v==='')return;
  out[f.key]=(f.type==='enum')?parseInt(v,10):v;});
 return out;}
function updateSummary(){const f=collectFields();
 const parts=Object.keys(f).map(k=>{const d=FIELDS.find(x=>x.key===k);
  let v=f[k];if(d.options){const o=d.options.find(o=>String(o[0])===String(v));v=o?o[1]:v;}
  return d.name+'='+v;});
 $('ed_summary').textContent=parts.length?parts.join('; '):'（未选择任何字段）';
 $('ed_btnApply').disabled=!parts.length||!lastCount;}
function render(){lastCount=curItems.length;
 const f=collectFields();
 const parts=Object.keys(f).map(k=>{const d=FIELDS.find(x=>x.key===k);
  let v=f[k];if(d.options){const o=d.options.find(o=>String(o[0])===String(v));v=o?o[1]:v;}
  return d.name+'='+v;});
 const summary=parts.length?parts.join('; '):'—';
 if(!curItems.length){selSet=new Set();updateCount();
  $('ed_rows').innerHTML='<tr><td colspan="5" class="empty">未找到符合条件的图片</td></tr>';return;}
 const known=curItems.every(it=>selSet.has(it.path));
 if(!known)selSet=new Set(curItems.map(it=>it.path));
 $('ed_rows').innerHTML=curItems.map(it=>{
  const fail=it.note&&it.note.startsWith('失败');
  return `<tr class="${fail?'fail':'ok'}"><td class="selcell"><input type="checkbox" class="ed_sel" data-path="${esc(it.path)}" ${selSet.has(it.path)?'checked':''}><button class="delrow" data-path="${esc(it.path)}" title="从清单中移除">×</button></td>`+
   `<td>${esc(it.name)}</td><td>${esc(it.rel_dir||'—')}</td>`+
   `<td>${esc(summary)}</td><td>${esc(it.note||'—')}</td></tr>`;}).join('');
 document.querySelectorAll('#ed_rows input.ed_sel').forEach(cb=>
  cb.addEventListener('change',()=>{const p=cb.dataset.path;
   if(cb.checked)selSet.add(p);else selSet.delete(p);updateCount();}));
 document.querySelectorAll('#ed_rows button.delrow').forEach(b=>
  b.addEventListener('click',()=>delRow(b.dataset.path)));
 updateCount();}
let selSet=new Set();
function updateCount(){lastCount=curItems.filter(it=>selSet.has(it.path)).length;
 const total=curItems.length;
 const selN=curItems.filter(it=>selSet.has(it.path)).length;
 const sa=$('ed_selAll');
 if(sa){sa.checked=total>0&&selN===total;
  sa.indeterminate=selN>0&&selN<total;}
 const sc=$('ed_selCount');
 if(sc)sc.textContent=total?('已勾选 '+selN+' / '+total+' 张'):'';
 updateApply();}
function opts(){return{folder:$('ed_folder').value.trim(),recursive:$('ed_recursive').checked};}
async function scan(){if(pgTimer){alert('正在修改中，请等待完成');return;}
 $('ed_msg').textContent='扫描中，请稍候…';
 try{const j=await post(API+'/scan',opts());
  if(!j.ok){$('ed_msg').textContent='扫描失败: '+j.error;log('扫描失败: '+j.error);return;}
  curItems=j.items;pickMode=false;pickList=[];render();updateApply();
  $('ed_msg').textContent='扫描完成: 共 '+j.stats.total+' 张图片';
  log($('ed_msg').textContent);
 }catch(e){$('ed_msg').textContent='请求失败: '+e;}}
let pickBusy=false;
async function pickFiles(){if(pgTimer){alert('正在修改中，请等待完成');return;}
 if(pickBusy)return;pickBusy=true;
 $('ed_msg').textContent='已弹出文件选择对话框：请在其中选图（Ctrl 多选），选好后回到本页面';
 try{const j=await fetch('/api/pickfiles',{method:'POST',
  headers:{'Content-Type':'application/json'},body:'{}'});
  const g=await j.json();
  if(!g.ok){$('ed_msg').textContent='打开文件选择器失败: '+(g.error||'');return;}
  if(!g.files.length){$('ed_msg').textContent='未选择任何图片';log('手动选择: 已取消');return;}
  const known=new Set(curItems.map(it=>it.path));
  const added=g.files.filter(p=>!known.has(p));
  if(!added.length){$('ed_msg').textContent='所选图片都已在清单中（共 '+curItems.length+' 张）';
   log('手动选择: 新增 0 张（全部重复），清单共 '+curItems.length+' 张');return;}
  added.forEach(p=>selSet.add(p));
  pickList=curItems.map(it=>it.path).concat(added);
  pickMode=true;
  curItems=curItems.concat(added.map(p=>({path:p,
   name:p.split('\\').pop().split('/').pop(),
   rel_dir:p.split('\\').slice(0,-1).pop()||p.split('/').slice(0,-1).pop()||'',note:''})));
  render();updateApply();
  $('ed_msg').textContent='手动选择完成: 新增 '+added.length+' 张，清单共 '+curItems.length+' 张';
  log('手动选择: 新增 '+added.length+' 张（重复 '+g.files.length+' 张未计入），清单共 '+curItems.length+' 张');
 }catch(e){$('ed_msg').textContent='请求失败: '+e;}
 finally{pickBusy=false;}}
function updateApply(){$('ed_btnApply').disabled=!(lastCount&&Object.keys(collectFields()).length);}
function setPg(text,done,total){$('ed_pgPhase').textContent=text;
 $('ed_pgNum').textContent=done+' / '+total;
 $('ed_pgBar').style.width=total>0?Math.round(done/total*100)+'%':'0%';}
function askApply(){const f=collectFields();
 if(!lastCount){$('ed_msg').textContent='请先扫描或手动选择图片';return;}
 if(!Object.keys(f).length){$('ed_msg').textContent='请先勾选要写入的 EXIF 字段并填写内容';return;}
 const sm=saveMode();
 $('ed_cfText').textContent='将对 '+lastCount+' 张图片写入 '+Object.keys(f).length+' 项 EXIF 字段。'+
  (sm==='inplace'?'原图片会先备份到 backup\\ 文件夹，然后原地修改。'
                 :'原图片保持不动，修改后的图片将另存到 changed\\ 文件夹。');
 $('ed_confirmBox').style.display='block';}
function cancelApply(){$('ed_confirmBox').style.display='none';}
async function doApply(){$('ed_confirmBox').style.display='none';
 const b=$('ed_btnApply');b.disabled=true;b.textContent='正在修改…';
 $('ed_btnScan').disabled=true;$('ed_btnPick').disabled=true;
 $('ed_pgBox').style.display='block';setPg('正在写入 EXIF 字段…',0,0);
 $('ed_msg').textContent='正在修改…';
 const body=Object.assign(opts(),{save_mode:saveMode(),fields:collectFields(),
  selected:Array.from(selSet)});
 if(pickMode)body.mode='files',body.files=curItems.map(it=>it.path);
 pgTimer=setInterval(async()=>{try{const r=await fetch(API+'/progress');const g=await r.json();
  if(!g.applying)return;
  if(g.phase==='writing')setPg('正在写入 EXIF 字段',g.done,g.total);
  else setPg('准备中…',0,0);}catch(e){}},500);
 try{const j=await post(API+'/apply',body,600000);
  if(!j.ok){$('ed_msg').textContent='失败: '+j.error;log('失败: '+j.error);
   setPg('失败: '+j.error,0,0);$('ed_pgBar').style.background='var(--err)';}
  else{curItems=j.items;render();
   $('ed_msg').textContent='修改完成: 成功 '+j.done+' 张'+(j.failed?', 失败 '+j.failed+' 张':'');
   setPg('✓ 修改完成: 成功 '+j.done+' 张'+(j.failed?', 失败 '+j.failed+' 张':''),j.done,Math.max(j.done,1));
   log($('ed_msg').textContent);
   if(saveMode()==='changed')log('原图片保持不变；修改后的图片已另存到 changed\\ 文件夹。');
   else log('照片已原地更新；原图片备份在 backup\\ 文件夹内。');}
 }catch(e){$('ed_msg').textContent='请求失败: '+e;
 setPg('请求失败，请重试',0,0);$('ed_pgBar').style.background='var(--err)';}
 finally{clearInterval(pgTimer);pgTimer=null;b.textContent='应用修改';
 b.disabled=!(lastCount&&Object.keys(collectFields()).length);
 $('ed_btnScan').disabled=false;$('ed_btnPick').disabled=false;
 setTimeout(()=>{$('ed_pgBox').style.display='none';$('ed_pgBar').style.background='var(--accent2)';},8000);}}
function shutdown(){if(!confirm('退出工具？'))return;
 fetch(API+'/shutdown',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}).catch(()=>{});
 document.body.innerHTML='<div class="wrap"><h1>已退出</h1><div class="sub">可以关闭此页面了。</div></div>';}
$('ed_btnScan').addEventListener('click',scan);
$('ed_btnPick').addEventListener('click',pickFiles);
$('ed_btnApply').addEventListener('click',askApply);
$('ed_btnCsv').addEventListener('click',()=>{location.href=API+'/report';});
$('ed_btnShutdown').addEventListener('click',shutdown);
$('ed_cfYes').addEventListener('click',doApply);
$('ed_cfNo').addEventListener('click',cancelApply);
$('ed_selAll').addEventListener('change',()=>{
 const on=$('ed_selAll').checked;
 curItems.forEach(it=>{if(on)selSet.add(it.path);else selSet.delete(it.path);});
 document.querySelectorAll('#ed_rows input.ed_sel').forEach(cb=>{cb.checked=on;});
 updateCount();});
function delRow(p){curItems=curItems.filter(it=>it.path!==p);selSet.delete(p);
 if(pickMode)pickList=curItems.map(it=>it.path);
 render();
 $('ed_msg').textContent='已从清单移除 1 张，剩 '+curItems.length+' 张';}
function clearAllEd(){if(!curItems.length)return;
 if(!confirm('清空全部清单？'))return;
 curItems=[];selSet.clear();pickMode=false;pickList=[];
 render();
 $('ed_msg').textContent='清单已清空';log('清单已清空');}
document.querySelectorAll('#ed_rows button.delrow').forEach(b=>
 b.addEventListener('click',()=>delRow(b.dataset.path)));
$('ed_clearAll').addEventListener('click',clearAllEd);
})();
</script>
</body>
</html>
'''


def launch_web():
    import webbrowser
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlparse

    ff_state = {'items': [], 'folder': '', 'applying': False,
                'pg_done': 0, 'pg_total': 0, 'pg_phase': ''}
    ed_state = {'items': [], 'folder': '', 'applying': False,
                'pg_done': 0, 'pg_total': 0, 'pg_phase': ''}
    log_path = os.path.join(self_dir(), 'exif_tool.log')

    def _alog(msg):
        try:
            with open(log_path, 'a', encoding='utf-8') as f:
                f.write('[%s] %s\n' % (_dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), msg))
        except OSError:
            pass

    default_folder = os.path.dirname(self_dir())
    if not os.path.isdir(default_folder):
        default_folder = self_dir()
    page = (PAGE.replace('__DEFAULT_FOLDER__', default_folder)
                .replace('__FF_HEIC__', 'checked' if HAS_HEIF else '')
                .replace('__FF_HEIC_HINT__', '' if HAS_HEIF else '（未装pillow-heif）')
                .replace('__FIELDS_JSON__', json.dumps(ED_FIELD_DEFS, ensure_ascii=False)))

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, body, ctype, no_store=False):
            self.send_response(200)
            self.send_header('Content-Type', ctype)
            if no_store:
                self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            n = int(self.headers.get('Content-Length') or 0)
            return json.loads(self.rfile.read(n).decode('utf-8')) if n else {}

        def _csv_report(self, items, fname):
            buf = io.StringIO()
            w = csv.writer(buf)
            if fname == 'exif_tool_ff_report.csv':
                w.writerow(['文件名', '所在文件夹', '路径', '拍摄日期', 'EXIF时间',
                            '文件修改时间', '将写入的时间', '操作', '备注'])
                for it in items:
                    w.writerow([it['name'], it.get('rel_dir', ''), it['path'],
                                show_dt(it.get('shot')), show_dt(it.get('exif_t')),
                                show_dt(it.get('mtime')), show_dt(it.get('new')),
                                it['action'], it.get('note', '')])
            else:
                w.writerow(['文件名', '所在文件夹', '路径', '状态'])
                for it in items:
                    w.writerow([it['name'], it.get('rel_dir', ''), it['path'],
                                it.get('note', '')])
            data = buf.getvalue().encode('utf-8-sig')
            self.send_response(200)
            self.send_header('Content-Type', 'text/csv; charset=utf-8')
            self.send_header('Content-Disposition', 'attachment; filename="%s"' % fname)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            p = urlparse(self.path).path
            if p in ('/', '/index.html'):
                _alog('GET / (page)')
                self._send(page.encode('utf-8'), 'text/html; charset=utf-8', no_store=True)
            elif p == '/api/ping':
                self._json({'ok': True})
            elif p == '/api/ff/progress':
                self._json({'ok': True, 'applying': ff_state['applying'],
                            'phase': ff_state['pg_phase'],
                            'done': ff_state['pg_done'], 'total': ff_state['pg_total']})
            elif p == '/api/ed/progress':
                self._json({'ok': True, 'applying': ed_state['applying'],
                            'phase': ed_state['pg_phase'],
                            'done': ed_state['pg_done'], 'total': ed_state['pg_total']})
            elif p == '/api/ff/report':
                self._csv_report(ff_state['items'], 'exif_tool_ff_report.csv')
            elif p == '/api/ed/report':
                self._csv_report(ed_state['items'], 'exif_tool_ed_report.csv')
            else:
                self._json({'ok': False, 'error': 'not found'}, 404)

        def do_POST(self):
            p = urlparse(self.path).path
            if p == '/api/pickfiles':
                try:
                    files = pick_images_dialog()
                    _alog('pickfiles -> %d selected' % len(files))
                    self._json({'ok': True, 'files': files})
                except Exception as e:
                    _alog('pickfiles FAILED: %s' % e)
                    self._json({'ok': False, 'error': str(e), 'files': []})
            elif p == '/api/ff/scan':
                o = self._body()
                folder = o.get('folder', '')
                if not folder or not os.path.isdir(folder):
                    self._json({'ok': False, 'error': '文件夹不存在'})
                    return
                try:
                    items, stats = ff_scan_folder(folder, o.get('recursive', True),
                                                  _ff_exts(o), exclude_dirs=[self_dir()])
                except Exception as e:
                    _alog('ff scan FAILED %s: %s' % (folder, e))
                    self._json({'ok': False, 'error': str(e)})
                    return
                ff_state.update(items=items, folder=folder)
                _alog('ff scan %s -> total=%d need_fix=%d' % (folder, stats['total'], stats['need_fix']))
                self._json({'ok': True, 'stats': stats,
                            'items': [ff_item_json(it) for it in items]})
            elif p == '/api/ff/analyze_files':
                o = self._body()
                files = [f for f in (o.get('files') or []) if os.path.isfile(f)]
                if not files:
                    self._json({'ok': False, 'error': '没有有效的图片文件'})
                    return
                results = []
                for fp in files:
                    ext = os.path.splitext(fp)[1].lower()
                    if ext not in SUPPORTED:
                        continue
                    try:
                        item = ff_make_plan(fp, ext)
                    except Exception as e:
                        item = {'path': fp, 'name': os.path.basename(fp),
                                'shot': None, 'exif_t': None, 'digit': None,
                                'mtime': None, 'new': None,
                                'action': ACT_ERR, 'note': str(e)}
                    item['rel_dir'] = os.path.basename(os.path.dirname(fp))
                    results.append(item)
                if not results:
                    self._json({'ok': False, 'error': '所选文件中没有支持的图片格式'})
                    return
                stats = ff_stats_of(results)
                ff_state.update(items=results)
                self._json({'ok': True, 'stats': stats,
                            'items': [ff_item_json(it) for it in results]})
            elif p == '/api/ff/apply':
                o = self._body()
                files_mode = o.get('mode') == 'files'
                folder = o.get('folder', '')
                if not files_mode and (not folder or not os.path.isdir(folder)):
                    self._json({'ok': False, 'error': '文件夹不存在'})
                    return
                if ff_state['applying']:
                    self._json({'ok': False, 'error': '已有任务在进行中，请稍候'})
                    return
                ff_state['applying'] = True
                _alog('ff apply START (%s)' % ('files' if files_mode else folder))
                try:
                    ff_state['pg_phase'] = 'preparing'
                    if files_mode:
                        items = []
                        for fp in (o.get('files') or []):
                            if not os.path.isfile(fp):
                                continue
                            ext = os.path.splitext(fp)[1].lower()
                            if ext not in SUPPORTED:
                                continue
                            try:
                                item = ff_make_plan(fp, ext)
                            except Exception as e2:
                                item = {'path': fp, 'name': os.path.basename(fp),
                                        'shot': None, 'exif_t': None, 'digit': None,
                                        'mtime': None, 'new': None,
                                        'action': ACT_ERR, 'note': str(e2)}
                            item['rel_dir'] = os.path.basename(os.path.dirname(fp))
                            items.append(item)
                    else:
                        items, _ = ff_scan_folder(folder, o.get('recursive', True),
                                                  _ff_exts(o), exclude_dirs=[self_dir()])
                    sel = o.get('selected')
                    if isinstance(sel, list):
                        if not sel:
                            self._json({'ok': False, 'error': '没有勾选任何照片，请先在清单中勾选'})
                            return
                        sel_set = set(sel)
                        items = [it for it in items if it['path'] in sel_set]
                    need = [it for it in items if it['action'] in (ACT_SYNC, ACT_FILL)]
                    ff_state['pg_done'], ff_state['pg_total'] = 0, len(need)

                    def _prog(cur, total):
                        ff_state['pg_done'], ff_state['pg_total'] = cur, total

                    ff_state['pg_phase'] = 'writing'
                    save_mode = o.get('save_mode', 'inplace')
                    done, failed = ff_apply_plans(items, folder,
                                                  save_mode=save_mode,
                                                  backup=o.get('backup', True),
                                                  sync_mtime=(o.get('sync_mtime', False)
                                                              and save_mode == 'inplace'),
                                                  progress=_prog,
                                                  backup_mode='sibling' if files_mode else 'root')
                    ff_state['pg_phase'] = 'rescanning'
                    if files_mode:
                        items2 = []
                        for fp in (o.get('files') or []):
                            if not os.path.isfile(fp):
                                continue
                            ext = os.path.splitext(fp)[1].lower()
                            if ext not in SUPPORTED:
                                continue
                            try:
                                item = ff_make_plan(fp, ext)
                            except Exception as e2:
                                item = {'path': fp, 'name': os.path.basename(fp),
                                        'shot': None, 'exif_t': None, 'digit': None,
                                        'mtime': None, 'new': None,
                                        'action': ACT_ERR, 'note': str(e2)}
                            item['rel_dir'] = os.path.basename(os.path.dirname(fp))
                            items2.append(item)
                        stats2 = ff_stats_of(items2)
                    else:
                        items2, stats2 = ff_scan_folder(folder, o.get('recursive', True),
                                                        _ff_exts(o), exclude_dirs=[self_dir()])
                    ff_state.update(items=items2)
                    _alog('ff apply -> done=%d failed=%d' % (done, failed))
                    self._json({'ok': True, 'done': done, 'failed': failed,
                                'stats': stats2,
                                'items': [ff_item_json(it) for it in items2]})
                except Exception as e:
                    _alog('ff apply FAILED: %s' % e)
                    self._json({'ok': False, 'error': str(e)})
                finally:
                    ff_state['applying'] = False
            elif p == '/api/ed/scan':
                o = self._body()
                folder = o.get('folder', '')
                if not folder or not os.path.isdir(folder):
                    self._json({'ok': False, 'error': '文件夹不存在'})
                    return
                try:
                    items, stats = ed_scan_folder(folder, o.get('recursive', True),
                                                  exclude_dirs=[self_dir()])
                except Exception as e:
                    _alog('ed scan FAILED %s: %s' % (folder, e))
                    self._json({'ok': False, 'error': str(e)})
                    return
                ed_state.update(items=items, folder=folder)
                _alog('ed scan %s -> total=%d' % (folder, stats['total']))
                self._json({'ok': True, 'stats': stats,
                            'items': [ed_item_json(it, '') for it in items]})
            elif p == '/api/ed/apply':
                o = self._body()
                folder = o.get('folder', '')
                files_mode = o.get('mode') == 'files'
                if not files_mode and (not folder or not os.path.isdir(folder)):
                    self._json({'ok': False, 'error': '文件夹不存在'})
                    return
                if ed_state['applying']:
                    self._json({'ok': False, 'error': '已有任务在进行中，请稍候'})
                    return
                try:
                    parsed = ed_parse_fields(o.get('fields') or {})
                except ValueError as e:
                    _alog('ed apply 字段解析失败: %s' % e)
                    self._json({'ok': False, 'error': str(e)})
                    return
                if not parsed:
                    self._json({'ok': False, 'error': '没有要写入的字段'})
                    return
                if files_mode:
                    items = []
                    for fp in (o.get('files') or []):
                        if not os.path.isfile(fp):
                            continue
                        ext = os.path.splitext(fp)[1].lower()
                        if ext not in SUPPORTED:
                            continue
                        items.append({'path': fp, 'name': os.path.basename(fp),
                                      'rel_dir': os.path.basename(os.path.dirname(fp)),
                                      'note': ''})
                    if not items:
                        self._json({'ok': False, 'error': '文件清单为空，请重新选择'})
                        return
                else:
                    items, _ = ed_scan_folder(folder, o.get('recursive', True),
                                              exclude_dirs=[self_dir()])
                    if not items:
                        self._json({'ok': False, 'error': '所选文件夹中没有支持的图片'})
                        return
                sel = o.get('selected')
                if isinstance(sel, list) and sel:
                    sel_set = set(sel)
                    items = [it for it in items if it['path'] in sel_set]
                if not items:
                    self._json({'ok': False, 'error': '没有勾选任何图片，请先在清单中勾选'})
                    return
                ed_state['applying'] = True
                ed_state['pg_phase'] = 'writing'
                ed_state['pg_done'], ed_state['pg_total'] = 0, len(items)
                _alog('ed apply START (%s, %d 项字段, %d 张)' %
                      (o.get('save_mode', 'inplace'), len(parsed), len(items)))

                def _prog(cur, total):
                    ed_state['pg_done'], ed_state['pg_total'] = cur, total

                try:
                    done, failed = ed_apply_items(items, folder, parsed,
                                                  save_mode=o.get('save_mode', 'inplace'),
                                                  progress=_prog)
                    ed_state.update(items=items)
                    _alog('ed apply -> done=%d failed=%d' % (done, failed))
                    self._json({'ok': True, 'done': done, 'failed': failed,
                                'items': [ed_item_json(it, '') for it in items]})
                except Exception as e:
                    _alog('ed apply FAILED: %s' % e)
                    self._json({'ok': False, 'error': str(e)})
                finally:
                    ed_state['applying'] = False
            elif p == '/api/shutdown':
                _alog('shutdown requested')
                self._json({'ok': True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self._json({'ok': False, 'error': 'not found'}, 404)

    class QuietServer(ThreadingHTTPServer):
        # Windows 上 SO_REUSEADDR 允许两个进程抢同一端口（连接随机分配），
        # 必须关闭，才能让端口被占时正确回退到下一个端口
        allow_reuse_address = False

    server = None
    for port in range(8780, 8800):
        try:
            server = QuietServer(('127.0.0.1', port), Handler)
            break
        except OSError:
            continue
    if server is None:
        print('无法绑定本地端口 (8780-8799 均被占用)')
        return 1
    url = 'http://127.0.0.1:%d' % port
    print('EXIF 信息工具已启动: %s' % url)
    print('在浏览器中操作；关闭本窗口或点击页面中的「退出工具」即可结束。')
    _alog('server started at %s' % url)
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def _ff_exts(o):
    exts = set()
    if o.get('jpg', True):
        exts |= JPEG_EXTS
    if o.get('png', True):
        exts |= PNG_EXTS
    if o.get('heic', False) and HAS_HEIF:
        exts |= HEIF_EXTS
    return exts


def main(argv):
    return launch_web()


if __name__ == '__main__':
    sys.exit(main(sys.argv))
