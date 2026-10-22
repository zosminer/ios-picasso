#!/usr/bin/env python3
"""从 picasso 原厂设备树源码里取出电池曲线，生成内核电量计用的 C 表。

  picasso-battery-lut.py PICASSO-MERGED.DTS OUT.h

读的是电池节点 qcom,4226072_Xiaomi_G7ANVTBM4P_4500mAh（K30 5G 的 4500 mAh 电池，
小米 QG 电量计用的曲线）：
  qcom,pc-temp-v1-lut / v2-lut   OCV（0.1 mV）随电量（0.01%）和温度（°C）
  qcom,fcc1-temp-lut / fcc2-lut  满电容量（mAh）随温度
输出给补丁 0085 的 bsd/dev/picasso_gauge_lut.h。DTS 用 dtc -I dtb -O dts 从原厂 boot.img
的 DTB 加 dtbo 合成（../../firmware/V13.0.3.0.SGICNXM/picasso-merged.dts）。
"""
import re, sys

NODE = 'qcom,4226072_Xiaomi_G7ANVTBM4P_4500mAh {'


def table(lines, start, name):
    for i in range(start, min(start + 600, len(lines))):
        if name + ' {' in lines[i]:
            block = '\n'.join(lines[i:i + 8])
            break
    else:
        raise SystemExit(f'{name}：电池节点里没有这张表')

    def cells(key):
        m = re.search(re.escape(key) + r' = <([^>]*)>', block)
        if not m:
            return None
        v = [int(x, 16) for x in m.group(1).split()]
        return [x - (1 << 32) if x >= 1 << 31 else x for x in v]
    return cells('qcom,lut-data'), cells('qcom,lut-row-legend'), cells('qcom,lut-col-legend')


def c_array(ctype, name, size, values, per):
    rows = ['\t' + ', '.join(str(v) for v in values[i:i + per]) + ','
            for i in range(0, len(values), per)]
    return f'static const {ctype} {name}[{size}] = {{\n' + '\n'.join(rows) + '\n};'


def main():
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    lines = open(sys.argv[1], encoding='utf-8', errors='replace').read().split('\n')
    start = next((i for i, l in enumerate(lines) if NODE in l), None)
    if start is None:
        raise SystemExit('找不到 4500 mAh 电池节点')
    v1, r1, c1 = table(lines, start, 'qcom,pc-temp-v1-lut')
    v2, r2, c2 = table(lines, start, 'qcom,pc-temp-v2-lut')
    f1, _, fc1 = table(lines, start, 'qcom,fcc1-temp-lut')
    f2, _, fc2 = table(lines, start, 'qcom,fcc2-temp-lut')
    if r1 != r2 or len(v1) != len(r1) * len(c1) or len(v2) != len(r2) * len(c2):
        raise SystemExit('两张 OCV 表的形状不一致')
    if r1 != sorted(r1, reverse=True) or r1[0] != 10000 or r1[-1] != 0:
        raise SystemExit('电量行不是从 100% 递减到 0%')
    if len(f1) != len(fc1) or len(f2) != len(fc2):
        raise SystemExit('满电容量表形状不对')
    out = [
        '/*',
        ' * 由 tools/picasso-battery-lut.py 从原厂设备树的',
        ' * qcom,4226072_Xiaomi_G7ANVTBM4P_4500mAh 节点生成，不要手改。',
        ' */',
        f'#define PG_ROWS {len(r1)}',
        f'#define PG_V1_COLS {len(c1)}',
        f'#define PG_V2_COLS {len(c2)}',
        f'#define PG_FCC1_COLS {len(fc1)}',
        f'#define PG_FCC2_COLS {len(fc2)}',
        '/* 行：电量，单位 0.01%（10000 = 100%），从高到低 */',
        c_array('int16_t', 'pg_rows', 'PG_ROWS', r1, 14),
        '/* qcom,pc-temp-v1-lut（原厂 lookup_soc_ocv 在充电时用）：列为温度 °C，数据为 OCV，单位 0.1 mV */',
        c_array('int16_t', 'pg_v1_cols', 'PG_V1_COLS', c1, 8),
        c_array('uint16_t', 'pg_v1', 'PG_ROWS * PG_V1_COLS', v1, len(c1)),
        '/* qcom,pc-temp-v2-lut（放电时用） */',
        c_array('int16_t', 'pg_v2_cols', 'PG_V2_COLS', c2, 8),
        c_array('uint16_t', 'pg_v2', 'PG_ROWS * PG_V2_COLS', v2, len(c2)),
        '/* qcom,fcc1-temp-lut（充电）/ fcc2-temp-lut（放电）：满电容量 mAh 随温度 */',
        c_array('int16_t', 'pg_fcc1_cols', 'PG_FCC1_COLS', fc1, 8),
        c_array('int16_t', 'pg_fcc1', 'PG_FCC1_COLS', f1, 8),
        c_array('int16_t', 'pg_fcc2_cols', 'PG_FCC2_COLS', fc2, 8),
        c_array('int16_t', 'pg_fcc2', 'PG_FCC2_COLS', f2, 8),
    ]
    open(sys.argv[2], 'w').write('\n'.join(out) + '\n')
    print(f'{sys.argv[2]}：{len(r1)} 行，充电表 {len(c1)} 列，放电表 {len(c2)} 列，'
          f'25 °C 满电容量 {f1[fc1.index(25)]}/{f2[fc2.index(25)]} mAh')


if __name__ == '__main__':
    main()
