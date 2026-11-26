// Remote Play side-channel test: connect with an existing registration, count video, log rumble / trigger
// effects, then send a short scripted left-stick and R2 sweep. Keys come from the environment (hex).
#include <chiaki/session.h>
#include <chiaki/log.h>
#include <chiaki/controller.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <stdatomic.h>
#include <time.h>

static atomic_int g_connected, g_quit; static atomic_long g_frames, g_bytes, g_rumbles, g_trig;
static FILE *g_logf, *g_dump;
static double now_s(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return t.tv_sec + t.tv_nsec / 1e9; }
static double g_t0;
static void log_cb(ChiakiLogLevel level, const char *msg, void *user) {
	if (g_logf) { fprintf(g_logf, "[%7.3f][%c] %s\n", now_s() - g_t0, chiaki_log_level_char(level), msg); fflush(g_logf); }
	if (level & (CHIAKI_LOG_ERROR | CHIAKI_LOG_WARNING)) fprintf(stderr, "[%7.3f][%c] %s\n", now_s() - g_t0, chiaki_log_level_char(level), msg);
}
static bool video_cb(uint8_t *buf, size_t size, int32_t lost, bool recovered, void *user) { g_frames++; g_bytes += (long)size; if (g_dump) fwrite(buf, 1, size, g_dump); return true; }
static void event_cb(ChiakiEvent *e, void *user) {
	double t = now_s() - g_t0;
	switch (e->type) {
	case CHIAKI_EVENT_CONNECTED: printf("[%7.3f] EVENT connected\n", t); g_connected = 1; break;
	case CHIAKI_EVENT_QUIT: printf("[%7.3f] EVENT quit: %s (%s)\n", t, chiaki_quit_reason_string(e->quit.reason), e->quit.reason_str ? e->quit.reason_str : ""); g_quit = 1; break;
	case CHIAKI_EVENT_RUMBLE: g_rumbles++; printf("[%7.3f] EVENT rumble left=%u right=%u\n", t, e->rumble.left, e->rumble.right); break;
	case CHIAKI_EVENT_TRIGGER_EFFECTS: g_trig++; printf("[%7.3f] EVENT trigger_effects typeL=%u typeR=%u R=[%u %u %u %u]\n", t, e->trigger_effects.type_left, e->trigger_effects.type_right, e->trigger_effects.right[0], e->trigger_effects.right[1], e->trigger_effects.right[2], e->trigger_effects.right[3]); break;
	case CHIAKI_EVENT_LOGIN_PIN_REQUEST: printf("[%7.3f] EVENT login pin requested (console has a login passcode)\n", t); break;
	case CHIAKI_EVENT_NICKNAME_RECEIVED: printf("[%7.3f] EVENT nickname %s\n", t, e->server_nickname); break;
	default: printf("[%7.3f] EVENT type %d\n", t, (int)e->type); break;
	}
	fflush(stdout);
}
static int hex2bin(const char *hex, uint8_t *out, size_t max) { size_t n = strlen(hex) / 2; if (n > max) return -1; for (size_t i = 0; i < n; i++) { unsigned v; if (sscanf(hex + 2 * i, "%2x", &v) != 1) return -1; out[i] = (uint8_t)v; } return (int)n; }
static const char *env(const char *k) { const char *v = getenv(k); if (!v || !*v) { fprintf(stderr, "missing env %s\n", k); exit(2); } return v; }

int main(int argc, char **argv) {
	const char *mode = argc > 1 ? argv[1] : "observe";   // observe | sweep
	int observe_s = argc > 2 ? atoi(argv[2]) : 8;
	g_t0 = now_s(); g_logf = fopen("rptest.log", "w"); if (getenv("RP_DUMP")) g_dump = fopen(getenv("RP_DUMP"), "wb");
	ChiakiLog log; chiaki_log_init(&log, CHIAKI_LOG_ALL & ~CHIAKI_LOG_VERBOSE, log_cb, NULL);
	if (chiaki_lib_init() != CHIAKI_ERR_SUCCESS) { fprintf(stderr, "lib init failed\n"); return 1; }
	ChiakiConnectInfo info; memset(&info, 0, sizeof(info));
	info.ps5 = true; info.host = env("RP_HOST");
	if (hex2bin(env("RP_REGIST_KEY_HEX"), (uint8_t *)info.regist_key, sizeof(info.regist_key)) < 0) return 2;
	if (hex2bin(env("RP_MORNING_HEX"), info.morning, sizeof(info.morning)) != 16) { fprintf(stderr, "bad morning\n"); return 2; }
	if (hex2bin(env("RP_ACCOUNT_HEX"), info.psn_account_id, sizeof(info.psn_account_id)) != 8) { fprintf(stderr, "bad account id\n"); return 2; }
	chiaki_connect_video_profile_preset(&info.video_profile, CHIAKI_VIDEO_RESOLUTION_PRESET_720p, CHIAKI_VIDEO_FPS_PRESET_30);
	info.video_profile_auto_downgrade = true; info.enable_keyboard = false; info.enable_dualsense = true;
	info.audio_video_disabled = CHIAKI_NONE_DISABLED; info.auto_regist = false; info.packet_loss_max = 0.05; info.enable_idr_on_fec_failure = true;
	ChiakiSession s;
	ChiakiErrorCode err = chiaki_session_init(&s, &info, &log);
	if (err != CHIAKI_ERR_SUCCESS) { fprintf(stderr, "session init: %s\n", chiaki_error_string(err)); return 1; }
	chiaki_session_set_event_cb(&s, event_cb, NULL);
	chiaki_session_set_video_sample_cb(&s, video_cb, NULL);
	if ((err = chiaki_session_start(&s)) != CHIAKI_ERR_SUCCESS) { fprintf(stderr, "session start: %s\n", chiaki_error_string(err)); return 1; }
	double t_wait = now_s();
	while (!g_connected && !g_quit && now_s() - t_wait < 30) usleep(50000);
	if (!g_connected) { printf("did not connect\n"); chiaki_session_stop(&s); chiaki_session_join(&s); chiaki_session_fini(&s); return 3; }
	ChiakiControllerState st; chiaki_controller_state_set_idle(&st); chiaki_session_set_controller_state(&s, &st);
	double t_c = now_s(); long f0 = 0;
	while (!g_quit && now_s() - t_c < observe_s) { usleep(1000000); long f = g_frames; printf("[%7.3f] video frames/s=%ld total=%ld bytes=%ld\n", now_s() - g_t0, f - f0, f, (long)g_bytes); fflush(stdout); f0 = f; }
	if (!g_quit && strcmp(mode, "sweep") == 0) {
		printf("[%7.3f] SWEEP start: left stick X right, left, center; then R2 ramp\n", now_s() - g_t0); fflush(stdout);
		double t_s = now_s();
		while (!g_quit) {
			double t = now_s() - t_s; chiaki_controller_state_set_idle(&st);
			if (t < 1.5) st.left_x = (int16_t)(32767 * (t / 1.5));
			else if (t < 3.0) st.left_x = (int16_t)(32767 - 65534 * ((t - 1.5) / 1.5));
			else if (t < 4.0) st.left_x = (int16_t)(-32767 * (1 - (t - 3.0)));
			else if (t < 6.0) st.r2_state = (uint8_t)(255 * ((t - 4.0) / 2.0));
			else if (t < 7.0) st.r2_state = 255;
			else if (t < 8.0) st.r2_state = (uint8_t)(255 * (1 - (t - 7.0)));
			else break;
			chiaki_session_set_controller_state(&s, &st); usleep(8000);
		}
		chiaki_controller_state_set_idle(&st); chiaki_session_set_controller_state(&s, &st);
		printf("[%7.3f] SWEEP done\n", now_s() - g_t0);
		double t_e = now_s(); while (!g_quit && now_s() - t_e < 3) usleep(100000);
	}
	printf("[%7.3f] summary: frames=%ld bytes=%ld rumble_events=%ld trigger_effect_events=%ld quit=%d\n", now_s() - g_t0, (long)g_frames, (long)g_bytes, (long)g_rumbles, (long)g_trig, (int)g_quit);
	chiaki_session_stop(&s); chiaki_session_join(&s); chiaki_session_fini(&s); if (g_dump) fclose(g_dump);
	return 0;
}
