/*
 * picasso 触控驱动纯函数部分（bsd/dev/picasso_touch_nvt.h，补丁 0082）的电脑端测试。
 *
 * 用法（先把补丁打到一份 xnu 源码上，取其中的头文件）：
 *   python3 tools/check-patch-series.py --xnu XNU --dxb DXB --keep /tmp/xnu-p
 *   cc -std=c11 -Wall -Wextra -I /tmp/xnu-p/bsd/dev tools/picasso-touch-test.c -o /tmp/pt-test
 *   /tmp/pt-test research/picasso-touch/novatek_nt36672c_g7a_fw0*.bin
 * 不带固件参数时只跑合成数据的测试。全部通过时退出码为 0。
 */
#include <stdint.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "picasso_touch_nvt.h"

static int failures;

#define CHECK(cond) do { \
	if (!(cond)) { \
		printf("失败 %s:%d: %s\n", __FILE__, __LINE__, #cond); \
		++failures; \
	} \
} while (0)

/* 造一个最小的合法固件：头部 0x100、ILM/DLM 各一段、0x30 起 13 个信息段（最后一个是 Header） */
static uint8_t *
make_fw(uint32_t size)
{
	uint8_t *d = calloc(1, size);
	uint32_t w[] = {0x100, 0x0, 0x13a77, 0x18100, 0x27140, 0x4197};
	for (unsigned i = 0; i < 6; ++i) {
		memcpy(d + 4 * i, &w[i], 4);
	}
	uint32_t crc[2] = {0x11111111, 0x22222222};
	memcpy(d + 0x18, crc, 8);
	uint32_t hdr[4] = {0x27040, 0xff, 0x0, 0x33333333};
	memcpy(d + 0xf0, hdr, 16);
	memcpy(d + 0x20000 - 3, "NVT", 3);
	d[0x20000 - 4096] = 0x14;
	d[0x20000 - 4096 + 1] = 0xeb;
	return d;
}

static void
test_synthetic_fw(void)
{
	const uint32_t size = 0x22000;
	uint8_t *d = make_fw(size);
	struct nvt_fw fw;
	CHECK(nvt_fw_parse(d, size, 2, &fw) == NVT_FW_OK);
	CHECK(fw.need == 0x20000 && fw.ver == 0x14);
	CHECK(fw.nparts == 15 && fw.ninfo == 13 && fw.novly == 0);
	CHECK(fw.part[0].bin == 0x100 && fw.part[0].sram == 0 && fw.part[0].size == 0x13a77);
	CHECK(fw.part[0].crc == 0x11111111 && fw.part[1].crc == 0x22222222);
	CHECK(fw.part[1].bin == 0x18100 && fw.part[1].sram == 0x27140 && fw.part[1].size == 0x4197);
	CHECK(fw.part[14].sram == 0x27040 && fw.part[14].size == 0xff && fw.part[14].bin == 0);
	CHECK(fw.part[14].crc == 0x33333333);
	/* MOD 结束标志同样有效 */
	memcpy(d + 0x20000 - 3, "MOD", 3);
	CHECK(nvt_fw_parse(d, size, 1, &fw) == NVT_FW_OK);
	/* 各种坏文件 */
	CHECK(nvt_fw_parse(d, 4095, 2, &fw) == NVT_FW_SHORT);
	CHECK(nvt_fw_parse(d, size, 0, &fw) == NVT_FW_NO_HWCRC);
	memcpy(d + 0x20000 - 3, "XYZ", 3);
	CHECK(nvt_fw_parse(d, size, 2, &fw) == NVT_FW_NO_FLAG);
	memcpy(d + 0x20000 - 3, "NVT", 3);
	d[0x20000 - 4096 + 1] = 0xea;
	CHECK(nvt_fw_parse(d, size, 2, &fw) == NVT_FW_BAD_VER);
	d[0x20000 - 4096 + 1] = 0xeb;
	uint32_t big = size + 1;
	memcpy(d, &big, 4);
	CHECK(nvt_fw_parse(d, size, 2, &fw) == NVT_FW_BAD_HEADER);
	uint32_t hdr_end = 0x100;
	memcpy(d, &hdr_end, 4);
	/* ILM 越过文件末尾（写 size+1 字节） */
	uint32_t ilm = size - 0x100;
	memcpy(d + 8, &ilm, 4);
	CHECK(nvt_fw_parse(d, size, 2, &fw) == NVT_FW_BAD_RANGE);
	ilm = size - 0x100 - 1;
	memcpy(d + 8, &ilm, 4);
	CHECK(nvt_fw_parse(d, size, 2, &fw) == NVT_FW_OK);
	/* 覆盖段描述越界 */
	d[0x28] = 0x1f;
	uint32_t dlm_bin = size - 8;
	memcpy(d + 12, &dlm_bin, 4);
	uint32_t zero = 0;
	memcpy(d + 20, &zero, 4);
	CHECK(nvt_fw_parse(d, size, 2, &fw) == NVT_FW_BAD_HEADER);
	free(d);
}

static void
test_real_fw(const char *path)
{
	FILE *f = fopen(path, "rb");
	if (!f) {
		perror(path);
		++failures;
		return;
	}
	fseek(f, 0, SEEK_END);
	long n = ftell(f);
	fseek(f, 0, SEEK_SET);
	uint8_t *d = malloc((size_t)n);
	if (fread(d, 1, (size_t)n, f) != (size_t)n) {
		perror(path);
		++failures;
	}
	fclose(f);
	struct nvt_fw fw;
	int e = nvt_fw_parse(d, (uint32_t)n, 2, &fw);
	printf("%s：%ld 字节，结果 %s，有效长度 %#x，版本 %u，%u 个分区\n", path, n,
	    nvt_fw_error(e), fw.need, fw.ver, fw.nparts);
	CHECK(e == NVT_FW_OK);
	uint32_t total = 0;
	for (unsigned i = 0; i < fw.nparts; ++i) {
		if (fw.part[i].size) {
			printf("  分区 %2u：SRAM %#07x 写 %#07x 字节，文件偏移 %#07x，CRC %#010x\n", i,
			    fw.part[i].sram, fw.part[i].size + 1, fw.part[i].bin, fw.part[i].crc);
			total += fw.part[i].size + 1;
		}
	}
	printf("  共写入 %u 字节\n", total);
	CHECK(fw.part[0].size && fw.part[1].size);
	free(d);
}

static void
test_trim(void)
{
	const uint8_t nt36675[6] = {0x0C, 0x12, 0x34, 0x72, 0x66, 0x03};
	const uint8_t nt36672a[6] = {0x0A, 0x00, 0x00, 0x72, 0x66, 0x03};
	const uint8_t nt36772[6] = {0x55, 0x00, 0x99, 0x00, 0x00, 0x00};
	const uint8_t other[6] = {0x00, 0x00, 0x00, 0x00, 0x00, 0x00};
	const uint8_t any675[6] = {0x01, 0x02, 0x03, 0x75, 0x66, 0x03};
	int t = nvt_trim_match(nt36675);
	CHECK(t == 0 && nvt_trim_table[t].map == &nvt_map_nt36675 && nvt_trim_table[t].hw_crc == 2);
	t = nvt_trim_match(nt36672a);
	CHECK(t == 6 && nvt_trim_table[t].map == &nvt_map_nt36672a && nvt_trim_table[t].hw_crc == 1);
	t = nvt_trim_match(nt36772);
	CHECK(t == 11 && nvt_trim_table[t].map == NULL);
	CHECK(nvt_trim_match(other) == -1);
	CHECK(nvt_trim_match(any675) == 1);
}

static void
test_points(void)
{
	uint8_t d[NVT_POINT_LEN] = {0};
	struct nvt_points p;
	/* 手指 ID 1 进入 (540,1200) */
	d[0] = (1 << 3) | 1; d[1] = 0x21; d[2] = 0x4B; d[3] = 0xC0;
	/* 手指 ID 3 移动 (1080,2400)：等于最大值，保留 */
	d[6] = (3 << 3) | 2; d[7] = 0x43; d[8] = 0x96; d[9] = 0x80;
	/* 手指 ID 5 抬起（状态 3）：忽略 */
	d[12] = (5 << 3) | 3; d[13] = 0x10; d[14] = 0x10; d[15] = 0x00;
	/* ID 0：忽略 */
	d[18] = 0x01;
	/* ID 7 坐标 x=1081 超出最大值：忽略 */
	d[24] = (7 << 3) | 1; d[25] = 0x43; d[26] = 0x10; d[27] = 0x90;
	/* ID 11：超出 10 个手指，忽略 */
	d[30] = (11 << 3) | 1;
	unsigned n = nvt_parse_points(d, 1080, 2400, &p);
	CHECK(n == 2 && p.mask == 0x5);
	CHECK(p.x[0] == 540 && p.y[0] == 1200);
	CHECK(p.x[2] == 1080 && p.y[2] == 2400);
	CHECK(nvt_scale(540, 1080, 1080) == 539);
	CHECK(nvt_scale(1080, 1080, 1080) == 1079);
	CHECK(nvt_scale(2400, 2400, 2400) == 2399);
	CHECK(nvt_scale(0, 2400, 2400) == 0);
	CHECK(nvt_scale(5000, 0, 1080) == 1079);
	memset(d, 0, sizeof(d));
	CHECK(nvt_parse_points(d, 1080, 2400, &p) == 0 && p.mask == 0);
	/* 看门狗帧与 ESD 心跳帧 */
	const uint8_t wdt[6] = {0xFD, 0xFE, 0xFD, 0xFD, 0xFE, 0xFE};
	const uint8_t esd[6] = {0x77, 0x77, 0x77, 0x77, 0x77, 0x77};
	const uint8_t nope[6] = {0x77, 0x77, 0x77, 0x77, 0x77, 0x76};
	CHECK(nvt_is_wdt_frame(wdt) && !nvt_is_esd_frame(wdt));
	CHECK(nvt_is_esd_frame(esd) && !nvt_is_wdt_frame(esd));
	CHECK(!nvt_is_esd_frame(nope));
}

static void
test_fwinfo(void)
{
	uint8_t d[16] = {0x14, 0xEB, 18, 32, 0x04, 0x38, 0x09, 0x60, 0, 0, 0, 0, 0, 0x08, 0, 0};
	struct nvt_fwinfo fi;
	CHECK(nvt_parse_fwinfo(d, &fi) == 1);
	CHECK(fi.ver == 0x14 && fi.x_num == 18 && fi.y_num == 32);
	CHECK(fi.xmax == 1080 && fi.ymax == 2400 && fi.type == 0x08);
	d[1] = 0;
	CHECK(nvt_parse_fwinfo(d, &fi) == 0);
}

static void
test_panel(void)
{
	static const char map[] = "37_02_0a=fw01\0" "37_02_0b=fw02\0" "36_02_0c=fw03";
	char out[8];
	CHECK(nvt_panel_lookup(map, sizeof(map), "37_02_0b", out, sizeof(out)) && !strcmp(out, "fw02"));
	CHECK(nvt_panel_lookup(map, sizeof(map), "36_02_0c", out, sizeof(out)) && !strcmp(out, "fw03"));
	/* 最后一项没有结尾 NUL 也能找到 */
	CHECK(nvt_panel_lookup(map, sizeof(map) - 1, "36_02_0c", out, sizeof(out)) && !strcmp(out, "fw03"));
	CHECK(!nvt_panel_lookup(map, sizeof(map), "37_02_0", out, sizeof(out)) && out[0] == 0);
	CHECK(!nvt_panel_lookup(map, sizeof(map), "37_02_0a_x", out, sizeof(out)));
	CHECK(!nvt_panel_lookup(map, sizeof(map), "", out, sizeof(out)));
	char tiny[3];
	CHECK(nvt_panel_lookup(map, sizeof(map), "37_02_0a", tiny, sizeof(tiny)) && !strcmp(tiny, "fw"));
}

int
main(int argc, char **argv)
{
	test_synthetic_fw();
	test_trim();
	test_points();
	test_fwinfo();
	test_panel();
	for (int i = 1; i < argc; ++i) {
		test_real_fw(argv[i]);
	}
	printf(failures ? "共 %d 项失败\n" : "全部通过\n", failures);
	return failures != 0;
}
