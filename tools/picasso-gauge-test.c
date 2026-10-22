/*
 * picasso 电量计算法（补丁 0085 的 bsd/dev/picasso_gauge.h）的电脑端测试。
 *   python3 tools/check-patch-series.py --xnu XNU --dxb DXB --keep /tmp/xnu-p
 *   cc -std=c11 -Wall -Wextra -I /tmp/xnu-p/bsd/dev tools/picasso-gauge-test.c -o /tmp/pg && /tmp/pg
 * 全部通过时退出码为 0。
 */
#include <stdint.h>
#include <stdio.h>
#include "picasso_gauge.h"

static int failures;
#define CHECK(c) do { if (!(c)) { printf("失败 %s:%d: %s\n", __FILE__, __LINE__, #c); ++failures; } } while (0)
#define NEAR(a, b, tol) ((a) - (b) <= (tol) && (b) - (a) <= (tol))

int
main(void)
{
	/* 1. 查表：表里的点原样返回，表外夹到 0 / 100% */
	CHECK(pg_ocv_to_soc(4384100, 250, 1) == 10000);           /* 充电表 25 °C 列 100% = 43841 */
	CHECK(pg_ocv_to_soc(4500000, 250, 0) == 10000);
	CHECK(pg_ocv_to_soc(2700000, 250, 0) == 0);   /* 放电表 0% 行最低 2.81 V */
	/* 单调：电压越高电量越高 */
	int prev = -1, mono = 1;
	for (int uv = 3300000; uv <= 4400000; uv += 5000) {
		int s = pg_ocv_to_soc(uv, 250, 0);
		if (s < prev) {
			mono = 0;
		}
		prev = s;
	}
	CHECK(mono);
	/* 3.85 V 左右应在 40%～60% 之间（锂电池中段） */
	int mid = pg_ocv_to_soc(3850000, 250, 0);
	printf("3.85 V 开路电压 -> %d.%02d%%\n", mid / 100, mid % 100);
	CHECK(mid > 4000 && mid < 6000);
	/* 温度在两列之间时介于两列结果之间 */
	int t10 = pg_ocv_to_soc(3800000, 100, 0), t25 = pg_ocv_to_soc(3800000, 250, 0);
	int t17 = pg_ocv_to_soc(3800000, 175, 0);
	CHECK((t17 >= t10 && t17 <= t25) || (t17 <= t10 && t17 >= t25));

	/* 2. 满电容量：25 °C 放电 4515、充电 4500（设备树原值） */
	CHECK(pg_fcc(250, 0) == 4515);
	CHECK(pg_fcc(250, 1) == 4500);
	CHECK(pg_fcc(-200, 1) == 4352);

	/* 3. 开机：SDAM 与电压接近 -> 用 SDAM；相差太大 -> 用电压 */
	struct pg_state s;
	int v50 = 0;
	for (int uv = 3500000; uv < 4300000; uv += 1000) {
		if (pg_ocv_to_soc(uv, 250, 0) >= 5000) {
			v50 = uv / 1000;
			break;
		}
	}
	printf("放电表 50%% 对应约 %d mV\n", v50);
	int soc = pg_start(&s, v50, 0, 1, 55, 0, 0);
	CHECK(s.source == 1 && soc == 5500);
	soc = pg_start(&s, v50, 0, 1, 90, 0, 0);
	CHECK(s.source == 2 && NEAR(soc, 5000, 60));
	soc = pg_start(&s, v50, 0, 0, 55, 0, 0);
	CHECK(s.source == 2);
	/* 学到的容量合理才用 */
	pg_start(&s, v50, 0, 1, 50, 120, 4200);
	CHECK(s.fcc_mah == 4200 && s.rbat_mohm == 120);
	pg_start(&s, v50, 0, 1, 50, 5000, 100);
	CHECK(s.fcc_mah == 4515 && s.rbat_mohm == PG_DEFAULT_RBAT_MOHM);

	/* 4. 放电带负载时端电压低，开路电压修正后不应低估太多 */
	pg_start(&s, v50 - 75, -500, 0, 0, 150, 0);   /* 500 mA × 150 mΩ = 75 mV 压降 */
	CHECK(NEAR(pg_soc(&s), 5000, 60));

	/* 5. 库仑计：4515 mAh，以 451.5 mA 放电 1 小时 = 10% */
	pg_start(&s, v50, 0, 1, 50, 0, 0);
	unsigned pct = 0;
	for (int i = 0; i < 360; ++i) {
		pct = pg_update(&s, v50 - 40, -452, 10000, 0, 0);
	}
	printf("放电 1 小时后：%u%%（内部 %d）\n", pct, pg_soc(&s));
	CHECK(pct == 40);
	/* 充电中、没充满最多 99% */
	pg_start(&s, 4300, 0, 1, 99, 0, 0);
	for (int i = 0; i < 360; ++i) {
		pct = pg_update(&s, 4380, 1000, 10000, 1, 0);
	}
	CHECK(pct == 99);
	/* 充电器报充满 = 100% */
	pct = pg_update(&s, 4380, 50, 10000, 1, 1);
	CHECK(pct == 100);
	/* 低于关机电压最多 1% */
	pg_start(&s, v50, 0, 1, 50, 0, 0);
	pct = pg_update(&s, 3350, -300, 10000, 0, 0);
	CHECK(pct == 1);

	/* 6. 静置修正：积分漂到 70%，实际开路电压是 50%，静置后逐步回到 50% 附近 */
	pg_start(&s, v50, 0, 1, 50, 0, 0);
	pg_set_soc(&s, 7000);
	for (int i = 0; i < 29; ++i) {       /* 不到 5 分钟不修正 */
		pct = pg_update(&s, v50, 0, 10000, 0, 0);
	}
	CHECK(pct == 70);
	for (int i = 0; i < 120; ++i) {      /* 再 20 分钟 */
		pct = pg_update(&s, v50, 0, 10000, 0, 0);
	}
	printf("静置修正后：%u%%\n", pct);
	CHECK(pct >= 49 && pct <= 51);
	/* 一次很长的间隔不会积出大跳变（最多按 10 分钟算） */
	pg_start(&s, v50, 0, 1, 50, 0, 0);
	pct = pg_update(&s, v50 - 40, -2000, 3600000u, 0, 0);
	CHECK(pct >= 42);

	printf(failures ? "共 %d 项失败\n" : "全部通过\n", failures);
	return failures != 0;
}
