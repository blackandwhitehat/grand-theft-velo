// Grand Theft Velo: pair with a PS5 for Remote Play.
// On the console: Settings > System > Remote Play > Link Device shows an 8-digit code.
// Environment: RP_HOST (console address), RP_ACCOUNT_HEX (16 hex characters, the PSN account id), RP_PIN.
// Prints one JSON object: {"ok":true,"regist_key_hex":..,"morning_hex":..,"nickname":..,"mac":..}
#include <chiaki/regist.h>
#include <chiaki/log.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static atomic_int g_done; static int g_ok;
static void log_cb(ChiakiLogLevel level, const char *msg, void *user) { (void)user; if (level & (CHIAKI_LOG_ERROR | CHIAKI_LOG_WARNING)) fprintf(stderr, "[%c] %s\n", chiaki_log_level_char(level), msg); }
static void regist_cb(ChiakiRegistEvent *e, void *user) {
	(void)user;
	if (e->type == CHIAKI_REGIST_EVENT_TYPE_FINISHED_SUCCESS && e->registered_host) {
		ChiakiRegisteredHost *h = e->registered_host; g_ok = 1;
		printf("{\"ok\":true,\"regist_key_hex\":\"");
		for (size_t i = 0; i < sizeof(h->rp_regist_key) && h->rp_regist_key[i]; i++) printf("%02x", (unsigned char)h->rp_regist_key[i]);
		printf("\",\"morning_hex\":\"");
		for (size_t i = 0; i < sizeof(h->rp_key); i++) printf("%02x", h->rp_key[i]);
		printf("\",\"nickname\":\"");
		for (const char *p = h->server_nickname; *p && p < h->server_nickname + sizeof(h->server_nickname); p++) if (*p != '"' && *p != '\\' && (unsigned char)*p >= 0x20) putchar(*p);
		printf("\",\"mac\":\"%02x%02x%02x%02x%02x%02x\"}\n", h->server_mac[0], h->server_mac[1], h->server_mac[2], h->server_mac[3], h->server_mac[4], h->server_mac[5]);
	} else printf("{\"ok\":false,\"error\":\"%s\"}\n", e->type == CHIAKI_REGIST_EVENT_TYPE_FINISHED_CANCELED ? "canceled" : "the console refused: check the code is current and the account matches");
	fflush(stdout); g_done = 1;
}
static int hex2bin(const char *hex, uint8_t *out, size_t n) { if (strlen(hex) != n * 2) return -1; for (size_t i = 0; i < n; i++) { unsigned v; if (sscanf(hex + 2 * i, "%2x", &v) != 1) return -1; out[i] = (uint8_t)v; } return 0; }

int main(void) {
	const char *host = getenv("RP_HOST"), *acc = getenv("RP_ACCOUNT_HEX"), *pin = getenv("RP_PIN");
	if (!host || !acc || !pin) { printf("{\"ok\":false,\"error\":\"missing RP_HOST, RP_ACCOUNT_HEX or RP_PIN\"}\n"); return 2; }
	ChiakiLog log; chiaki_log_init(&log, CHIAKI_LOG_ALL & ~CHIAKI_LOG_VERBOSE, log_cb, NULL);
	if (chiaki_lib_init() != CHIAKI_ERR_SUCCESS) { printf("{\"ok\":false,\"error\":\"library init failed\"}\n"); return 1; }
	ChiakiRegistInfo info; memset(&info, 0, sizeof(info));
	info.target = CHIAKI_TARGET_PS5_1; info.host = host; info.broadcast = false; info.psn_online_id = NULL; info.pin = (uint32_t)strtoul(pin, NULL, 10);
	if (hex2bin(acc, info.psn_account_id, CHIAKI_PSN_ACCOUNT_ID_SIZE) != 0) { printf("{\"ok\":false,\"error\":\"bad account id\"}\n"); return 2; }
	ChiakiRegist regist;
	if (chiaki_regist_start(&regist, &log, &info, regist_cb, NULL) != CHIAKI_ERR_SUCCESS) { printf("{\"ok\":false,\"error\":\"could not start registration\"}\n"); return 1; }
	for (int i = 0; i < 300 && !g_done; i++) usleep(100000);
	if (!g_done) { chiaki_regist_stop(&regist); printf("{\"ok\":false,\"error\":\"no answer from the console within 30 seconds\"}\n"); }
	chiaki_regist_fini(&regist);
	return g_ok ? 0 : 1;
}
