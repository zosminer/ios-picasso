/*
 * picasso 新增：在电脑上测试 fdt.c 的新函数（fdt_memory_banks、fdt_node_reg）。
 * 用法：fdt_host_test 设备树.dtb 期望区段数 [期望基址1 期望大小1 ...]
 * 例：fdt_host_test filled.dtb 2 0x80000000 0x80000000 0x100000000 0x180000000
 * 打印读到的内容；与期望不符时退出码为 1。
 */
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include "fdt.h"

/* 在内存里拼一个只有 / 和 /chosen（带 bootargs）的设备树 */
static size_t make_fdt(uint8_t *b, const char *bootargs)
{
	static const char strs[] = "bootargs";
	uint32_t *w = (uint32_t *)(b + 40);
	size_t n = 0, len = strlen(bootargs) + 1;
	w[n++] = __builtin_bswap32(1);			/* BEGIN_NODE "" */
	w[n++] = 0;
	w[n++] = __builtin_bswap32(1);			/* BEGIN_NODE "chosen" */
	memcpy(&w[n], "chosen\0\0", 8);
	n += 2;
	w[n++] = __builtin_bswap32(3);			/* PROP bootargs */
	w[n++] = __builtin_bswap32((uint32_t)len);
	w[n++] = 0;
	memset(&w[n], 0, (len + 3) & ~3UL);
	memcpy(&w[n], bootargs, len);
	n += (len + 3) / 4;
	w[n++] = __builtin_bswap32(2);			/* END_NODE chosen */
	w[n++] = __builtin_bswap32(2);			/* END_NODE / */
	w[n++] = __builtin_bswap32(9);			/* END */
	uint32_t off_struct = 40, off_strings = 40 + (uint32_t)(n * 4);
	memcpy(b + off_strings, strs, sizeof(strs));
	uint32_t hdr[10] = {0xd00dfeed, off_strings + sizeof(strs), off_struct, off_strings, 40, 17, 16, 0,
		sizeof(strs), (uint32_t)(n * 4)};
	for (int i = 0; i < 10; i++)
		((uint32_t *)b)[i] = __builtin_bswap32(hdr[i]);
	return off_strings + sizeof(strs);
}

static int test_panel(void)
{
	static uint8_t b[4096] __attribute__((aligned(8)));
	char out[16];
	int fail = 0;
	make_fdt(b, "console=ttyMSM0 msm_drm.dsi_display0=qcom,mdss_dsi_g7a_37_02_0b_dsc_video:config0:timing0 androidboot.x=1");
	fail |= !fdt_chosen_panel((uintptr_t)b, out, sizeof(out)) || strcmp(out, "37_02_0b");
	make_fdt(b, "msm_drm.dsi_display0=qcom,mdss_dsi_g7a_36_02_0c_dsc_video:");
	fail |= !fdt_chosen_panel((uintptr_t)b, out, sizeof(out)) || strcmp(out, "36_02_0c");
	make_fdt(b, "msm_drm.dsi_display0=qcom,mdss_dsi_sw43404_amoled_wqhd_cmd:");
	fail |= fdt_chosen_panel((uintptr_t)b, out, sizeof(out));
	make_fdt(b, "x mdss_dsi_g7a_");
	fail |= fdt_chosen_panel((uintptr_t)b, out, sizeof(out));
	char tiny[4];
	make_fdt(b, "mdss_dsi_g7a_37_02_0a_dsc_video");
	fail |= !fdt_chosen_panel((uintptr_t)b, tiny, sizeof(tiny)) || strcmp(tiny, "37_");
	printf("面板解析自测：%s\n", fail ? "失败" : "通过");
	return fail;
}

int main(int argc, char **argv)
{
	if (argc < 3) {
		fprintf(stderr, "用法：%s 设备树.dtb 期望区段数 [基址 大小 ...]\n", argv[0]);
		return 2;
	}
	FILE *f = fopen(argv[1], "rb");
	if (!f) {
		perror(argv[1]);
		return 2;
	}
	static unsigned char buf[4 << 20] __attribute__((aligned(8)));
	size_t n = fread(buf, 1, sizeof(buf), f);
	fclose(f);
	if (n == 0 || n == sizeof(buf)) {
		fprintf(stderr, "设备树为空或超过 4 MiB\n");
		return 2;
	}
	uint64_t base[8], size[8];
	int banks = fdt_memory_banks((uintptr_t)buf, base, size, 8);
	int fail = banks != atoi(argv[2]);
	printf("DRAM 区段：%d 个\n", banks);
	for (int i = 0; i < banks; i++) {
		printf("  区段 %d：基址 %#llx 大小 %#llx\n", i, (unsigned long long)base[i], (unsigned long long)size[i]);
		if (argc > 4 + 2 * i) {
			uint64_t eb = strtoull(argv[3 + 2 * i], NULL, 0), es = strtoull(argv[4 + 2 * i], NULL, 0);
			if (eb != base[i] || es != size[i])
				fail = 1;
		}
	}
	uint64_t rb, rs;
	if (fdt_node_reg((uintptr_t)buf, "qcom,rmtfs_sharedmem", &rb, &rs))
		printf("RMTFS：基址 %#llx 大小 %#llx\n", (unsigned long long)rb, (unsigned long long)rs);
	else
		printf("RMTFS：未找到\n");
	uint64_t cb = 0;
	if (!fdt_chosen_u64((uintptr_t)buf, "linux,initrd-start", &cb))
		printf("initrd-start：未设置（原厂设备树里本来就没有，ABL 启动时才写入）\n");
	else
		printf("initrd-start：%#llx\n", (unsigned long long)cb);
	char panel[16];
	if (fdt_chosen_panel((uintptr_t)buf, panel, sizeof(panel)))
		printf("面板：%s\n", panel);
	else
		printf("面板：bootargs 里没有（原厂设备树里本来就没有，ABL 启动时才写入）\n");
	fail |= test_panel();
	printf(fail ? "结果：与期望不符\n" : "结果：符合期望\n");
	return fail;
}
