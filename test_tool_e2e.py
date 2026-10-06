# -*- coding: utf-8 -*-
"""EXIF 信息工具（合并版）端到端测试：ff（时间修复）+ ed（信息修改）两模块。
临时测试图片，不触碰用户照片。"""
import os
import sys
import json
import shutil
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from PIL import Image
import piexif
import exif_tool as et

urllib.request.install_opener(
    urllib.request.build_opener(urllib.request.ProxyHandler({})))

TDIR = r'C:\Users\stfizer\AppData\Local\Temp\exif_tool_ff_test'
TDIR2 = r'C:\Users\stfizer\AppData\Local\Temp\exif_tool_ed_test'
URL = 'http://127.0.0.1:8780'
FAILS = []


def expect(c, m):
    print(('  PASS  ' if c else '  FAIL  ') + m)
    if not c:
        FAILS.append(m)


def post(path, obj, timeout=120):
    req = urllib.request.Request(URL + path, data=json.dumps(obj).encode('utf-8'),
                                 headers={'Content-Type': 'application/json'},
                                 method='POST')
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


EXIF_D = lambda: {'0th': {et.FF_TAG_DATETIME: b'2025:01:01 10:00:00'},
                  'Exif': {36867: b'2020:01:01 09:30:00',
                           36868: b'2020:01:01 09:30:00'},
                  'GPS': {}, '1st': {}, 'Interop': {}, 'thumbnail': None}


def setup():
    for d in (TDIR, TDIR2):
        if os.path.exists(d):
            shutil.rmtree(d)
        os.makedirs(d)
    Image.effect_noise((200, 150), 30).convert('RGB').save(
        os.path.join(TDIR, 'a.jpg'), exif=piexif.dump(EXIF_D()))
    Image.effect_noise((200, 150), 10).convert('RGB').save(os.path.join(TDIR, 'b.png'))
    Image.effect_noise((200, 150), 40).convert('RGB').save(os.path.join(TDIR2, 'e1.jpg'))
    Image.effect_noise((200, 150), 40).convert('RGB').save(os.path.join(TDIR2, 'e2.png'))


def read_exif(path):
    ext = os.path.splitext(path)[1].lower()
    d, _ = et.load_exif_dict(path, ext)
    return d


ED_FIELDS = {'Make': 'Apple', 'Model': 'iPad mini 2', 'Software': '12.5.5',
             'DateTimeOriginal': '2021/12/19 00:37:36', 'Flash': 32,
             'FocalLength': '3.3', 'FocalLengthIn35mmFilm': '32',
             'ExposureTime': '1/25', 'ExposureBiasValue': '0.00',
             'FNumber': 'f/2.4', 'ISOSpeedRatings': '320',
             'ExposureProgram': 2, 'MeteringMode': 5, 'Orientation': 1}


def main():
    setup()
    print('=== FF 模块：EXIF 时间修复 ===')
    j = post('/api/ff/scan', {'folder': TDIR, 'recursive': True,
                              'jpg': True, 'png': True, 'heic': False})
    expect(j['ok'] and j['stats']['need_fix'] == 2, 'ff scan 2 张待修改')
    expect(all(it.get('rel_dir') == '' for it in j['items']), '根层文件 rel_dir 为空')
    j = post('/api/ff/apply', {'folder': TDIR, 'recursive': True, 'jpg': True,
                               'png': True, 'heic': False, 'save_mode': 'inplace',
                               'backup': True, 'sync_mtime': False})
    expect(j['ok'] and j['done'] == 2 and j['failed'] == 0, 'ff apply 成功')
    expect(os.path.isfile(os.path.join(TDIR, 'backup', 'a.jpg')), 'ff backup/ 备份存在')
    d = read_exif(os.path.join(TDIR, 'a.jpg'))
    expect(d['Exif'].get(36867) == b'2020:01:01 09:30:00', 'ff 原地修改生效')

    print('=== FF 勾选：只应用勾选的图片 ===')
    TDIR3 = r'C:////Users////stfizer////AppData////Local////Temp////exif_tool_sel_test'
    if os.path.exists(TDIR3):
        shutil.rmtree(TDIR3)
    os.makedirs(TDIR3)
    Image.effect_noise((120, 90), 20).convert('RGB').save(os.path.join(TDIR3, 's1.jpg'))
    Image.effect_noise((120, 90), 20).convert('RGB').save(os.path.join(TDIR3, 's2.png'))
    sel_only = [os.path.join(TDIR3, 's1.jpg')]
    j = post('/api/ff/apply', {'folder': TDIR3, 'recursive': True, 'jpg': True,
                               'png': True, 'heic': False, 'save_mode': 'inplace',
                               'backup': False, 'sync_mtime': False,
                               'selected': sel_only})
    expect(j['ok'] and j['done'] == 1, 'ff 只处理勾选的 1 张 (done=%s)' % j.get('done'))
    d = read_exif(os.path.join(TDIR3, 's2.png'))
    expect(d['Exif'].get(36867) is None, '未勾选的 s2.png 未被修改')
    d = read_exif(os.path.join(TDIR3, 's1.jpg'))
    expect(d['Exif'].get(36867) is not None, '勾选的 s1.jpg 已被填充')
    j = post('/api/ff/apply', {'folder': TDIR3, 'recursive': True, 'jpg': True,
                               'png': True, 'heic': False, 'save_mode': 'inplace',
                               'backup': False, 'selected': []})
    expect(j['ok'] is False and '勾选' in j.get('error', ''), '空勾选被拒绝: %s' % j.get('error'))

    print('=== ED 模块：EXIF 信息修改 ===')
    j = post('/api/ed/scan', {'folder': TDIR2, 'recursive': True})
    expect(j['ok'] and j['stats']['total'] == 2, 'ed scan 2 张')
    j = post('/api/ed/apply', {'folder': TDIR2, 'recursive': True,
                               'save_mode': 'inplace', 'fields': ED_FIELDS,
                               'selected': [os.path.join(TDIR2, 'e1.jpg')]})
    expect(j['ok'] and j['done'] == 1, 'ed 只处理勾选的 1 张')
    d2chk = read_exif(os.path.join(TDIR2, 'e2.png'))
    expect(d2chk['0th'].get(0x010F) is None, '未勾选的 e2.png 未被写入')
    j = post('/api/ed/apply', {'folder': TDIR2, 'recursive': True,
                               'save_mode': 'inplace', 'fields': ED_FIELDS})
    expect(j['ok'] and j['done'] == 2 and j['failed'] == 0, 'ed apply 成功')
    d1 = read_exif(os.path.join(TDIR2, 'e1.jpg'))
    ok1 = (d1['0th'].get(0x010F) == b'Apple'
           and d1['0th'].get(0x0110) == b'iPad mini 2'
           and d1['0th'].get(0x0112) == 1
           and d1['Exif'].get(0x9209) == 32
           and d1['Exif'].get(36867) == b'2021:12:19 00:37:36'
           and d1['Exif'].get(0x829A) == (1, 25)
           and d1['Exif'].get(0x8827) == 320)
    expect(ok1, 'e1.jpg 14 项字段写入读回正确')
    d2 = read_exif(os.path.join(TDIR2, 'e2.png'))
    expect(d2['0th'].get(0x010F) == b'Apple' and d2['Exif'].get(0x9209) == 32,
           'e2.png 字段写入正确')

    print('=== ED changed 方式：原文件不动 ===')
    before = open(os.path.join(TDIR2, 'e1.jpg'), 'rb').read()
    j = post('/api/ed/apply', {'folder': TDIR2, 'recursive': True,
                               'save_mode': 'changed', 'fields': ED_FIELDS})
    expect(j['ok'] and j['done'] == 2, 'ed changed 应用成功')
    expect(open(os.path.join(TDIR2, 'e1.jpg'), 'rb').read() == before,
           '原图片字节级保持不变')
    expect(os.path.isfile(os.path.join(TDIR2, 'changed', 'e1.jpg')),
           '修改后的图片在 changed/')

    print('=== 共享：文件选择对话框端点 ===')
    j = post('/api/pickfiles', {})
    expect(j.get('ok') is True, '/api/pickfiles 端点可用（返回空=取消）')

    print()
    for d in (TDIR, TDIR2, TDIR3):
        shutil.rmtree(d, ignore_errors=True)
    if FAILS:
        print('结果: %d 项未通过' % len(FAILS))
        return 1
    print('结果: 合并版端到端测试全部通过 ✓')
    return 0


if __name__ == '__main__':
    sys.exit(main())
