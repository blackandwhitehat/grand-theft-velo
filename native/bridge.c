// Grand Theft Velo bridge: merges bike telemetry and an optional gamepad into ONE Remote Play controller.
//
// It listens for text lines on UDP (127.0.0.1 by default) and holds a PS5 Remote Play session while riding
// is enabled and someone is active. It is started and fed by the gtv service; it can also be run by hand.
//
// Input lines (space separated key=value):
//   bike power=<W> cadence=<rpm> steer=<deg, + is right> brake=<0|1>
//   pad  lx= ly= rx= ry=<-32768..32767> l2= r2=<0..255> buttons=<chiaki bitmask, decimal>
//   cfg  enabled=<0|1> full_watts= min_watts= sprint_watts= sprint_hz= steer_full= steer_deadzone= idle_s= stop_brake_s=
//   ctl  wake | poke | stop | quit
//   remote lx= ly= rx= ry= l2= r2= buttons= ms=<hold time>   (takes over completely: bike and gamepad are ignored until it lapses)
// Output: one JSON object per line on stdout (status every 0.5 s, plus events).
//
// Environment: RP_HOST, RP_REGIST_KEY_HEX, RP_MORNING_HEX, RP_ACCOUNT_HEX (the console pairing),
//   BRIDGE_VIDEO_CMD (a shell command that is fed the raw video stream on its standard input, for example ffmpeg),
//   BRIDGE_PORT (9750), BRIDGE_BIND (127.0.0.1), DRY_RUN=1 (never connect), and the cfg keys in upper case.
#include <chiaki/session.h>
#include <chiaki/discovery.h>
#include <chiaki/log.h>
#include <chiaki/controller.h>
#include <arpa/inet.h>
#include <sys/socket.h>
#include <sys/select.h>
#include <math.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <stdatomic.h>
#include <time.h>

static double now_s(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return t.tv_sec + t.tv_nsec / 1e9; }
static double envf(const char *k, double d) { const char *v = getenv(k); return v && *v ? atof(v) : d; }
static int hex2bin(const char *hex, uint8_t *out, size_t max) { size_t n = strlen(hex) / 2; if (n > max) return -1; for (size_t i = 0; i < n; i++) { unsigned v; if (sscanf(hex + 2 * i, "%2x", &v) != 1) return -1; out[i] = (uint8_t)v; } return (int)n; }

typedef struct { double power, cadence, steer; int brake; double at; } Bike;
typedef struct { int lx, ly, rx, ry, l2, r2; unsigned buttons; double at; } Pad;
typedef struct { int enabled; double full_w, min_w, sprint_w, sprint_hz, steer_full, steer_dz, idle_s, stop_brake_s; } Cfg;

static int has_key(const char *line, const char *key) {
	size_t n = strlen(key); const char *p = line;
	while ((p = strstr(p, key))) { if ((p == line || p[-1] == ' ') && p[n] == '=') return 1; p += n; }
	return 0;
}
static double kv(const char *line, const char *key, double d) {
	size_t n = strlen(key); const char *p = line;
	while ((p = strstr(p, key))) { if ((p == line || p[-1] == ' ') && p[n] == '=') return atof(p + n + 1); p += n; }
	return d;
}
static int clampi(double v, int lo, int hi) { return v < lo ? lo : v > hi ? hi : (int)v; }

// The gamepad passes through. The bike adds R2 from power, steering on left X, L2 from the brake and Cross
// taps when sprinting. Where both give a value the larger deflection wins. Stale sources drop out.
// With no brake switch, stopping pedaling is the brake: L2 is held for stop_brake_s seconds, then let go so
// the game does not start reversing.
static void merge(const Bike *b, const Pad *p, const Pad *remote, const Cfg *m, double t, ChiakiControllerState *st) {
	chiaki_controller_state_set_idle(st);
	if (t < remote->at) { b = NULL; p = remote; }      // remote control: "at" is when it lapses
	int bike_live = b && t - b->at < 2.0, pad_live = !b || t - p->at < 1.0;
	if (pad_live) { st->left_x = (int16_t)clampi(p->lx, -32768, 32767); st->left_y = (int16_t)clampi(p->ly, -32768, 32767); st->right_x = (int16_t)clampi(p->rx, -32768, 32767); st->right_y = (int16_t)clampi(p->ry, -32768, 32767); st->l2_state = (uint8_t)clampi(p->l2, 0, 255); st->r2_state = (uint8_t)clampi(p->r2, 0, 255); st->buttons = p->buttons & 0xFFFF; }
	if (bike_live) {
		int pedaling = b->cadence > 5 && b->power >= m->min_w;
		double span = m->full_w - m->min_w; if (span < 1) span = 1;
		int r2 = pedaling ? clampi(255.0 * (b->power - m->min_w) / span, 1, 255) : 0;
		if (r2 > st->r2_state) st->r2_state = (uint8_t)r2;
		// The dead zone is taken off the angle, so steering starts from zero at its edge instead of jumping.
		double s = isfinite(b->steer) ? b->steer : 0, mag = fabs(s) - m->steer_dz, full = m->steer_full - m->steer_dz;
		double a = mag <= 0 ? 0 : (s < 0 ? -mag : mag);
		int sx = clampi(32767.0 * a / (full < 1 ? 1 : full), -32767, 32767);
		if (abs(sx) > abs(st->left_x)) st->left_x = (int16_t)sx;
		if (b->brake) st->l2_state = 255;
		static int was_pedaling; static double stopped_at = -1e9;
		if (pedaling) was_pedaling = 1; else if (was_pedaling) { was_pedaling = 0; stopped_at = t; }
		if (!pedaling && m->stop_brake_s > 0 && t - stopped_at > 0.4 && t - stopped_at < 0.4 + m->stop_brake_s) st->l2_state = 255;
		if (pedaling && m->sprint_w > 0 && b->power >= m->sprint_w && fmod(t * m->sprint_hz, 1.0) < 0.5) st->buttons |= CHIAKI_CONTROLLER_BUTTON_CROSS;
	}
	if (st->l2_state) st->buttons |= CHIAKI_CONTROLLER_ANALOG_BUTTON_L2;
	if (st->r2_state) st->buttons |= CHIAKI_CONTROLLER_ANALOG_BUTTON_R2;
}

static atomic_int g_connected, g_quit, g_stop, g_rumble_l, g_rumble_r; static atomic_long g_frames; static double g_t0; static FILE *g_logf;
static char g_quit_reason[96]; static FILE *g_video;
static void on_sig(int s) { (void)s; g_stop = 1; }
static void log_cb(ChiakiLogLevel level, const char *msg, void *user) { (void)user; if (g_logf && (level & (CHIAKI_LOG_ERROR | CHIAKI_LOG_WARNING | CHIAKI_LOG_INFO))) { fprintf(g_logf, "[%9.3f][%c] %s\n", now_s() - g_t0, chiaki_log_level_char(level), msg); fflush(g_logf); } }
static bool video_cb(uint8_t *buf, size_t size, int32_t lost, bool rec, void *user) { (void)lost; (void)rec; (void)user; g_frames++; if (g_video && (fwrite(buf, 1, size, g_video) != size || fflush(g_video))) { pclose(g_video); g_video = NULL; } return true; }
static void event_cb(ChiakiEvent *e, void *user) {
	(void)user;
	if (e->type == CHIAKI_EVENT_CONNECTED) { g_connected = 1; printf("{\"event\":\"connected\"}\n"); }
	else if (e->type == CHIAKI_EVENT_QUIT) { snprintf(g_quit_reason, sizeof(g_quit_reason), "%s", chiaki_quit_reason_string(e->quit.reason)); g_quit = 1; printf("{\"event\":\"ended\",\"reason\":\"%s\"}\n", g_quit_reason); }
	else if (e->type == CHIAKI_EVENT_RUMBLE) { g_rumble_l = e->rumble.left; g_rumble_r = e->rumble.right; printf("{\"event\":\"rumble\",\"left\":%u,\"right\":%u}\n", e->rumble.left, e->rumble.right); }
	else if (e->type == CHIAKI_EVENT_TRIGGER_EFFECTS) printf("{\"event\":\"trigger\",\"type_r\":%u,\"r\":[%u,%u,%u,%u]}\n", e->trigger_effects.type_right, e->trigger_effects.right[0], e->trigger_effects.right[1], e->trigger_effects.right[2], e->trigger_effects.right[3]);
	else if (e->type == CHIAKI_EVENT_LOGIN_PIN_REQUEST) printf("{\"event\":\"login_pin_needed\"}\n");
	fflush(stdout);
}

int main(void) {
	g_t0 = now_s(); setvbuf(stdout, NULL, _IOLBF, 0); signal(SIGINT, on_sig); signal(SIGTERM, on_sig); signal(SIGPIPE, SIG_IGN);
	Cfg m = { (int)envf("ENABLED", 0), envf("FULL_WATTS", 250), envf("MIN_WATTS", 20), envf("SPRINT_WATTS", 400), envf("SPRINT_HZ", 6), envf("STEER_FULL", 30), envf("STEER_DEADZONE", 1.5), envf("IDLE_S", 300), envf("STOP_BRAKE_S", 0) };
	int dry = (int)envf("DRY_RUN", 0); int port = (int)envf("BRIDGE_PORT", 9750);
	const char *bind_ip = getenv("BRIDGE_BIND") && *getenv("BRIDGE_BIND") ? getenv("BRIDGE_BIND") : "127.0.0.1";
	int fd = socket(AF_INET, SOCK_DGRAM, 0); int one = 1; setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
	struct sockaddr_in a; memset(&a, 0, sizeof(a)); a.sin_family = AF_INET; a.sin_port = htons(port); inet_pton(AF_INET, bind_ip, &a.sin_addr);
	if (bind(fd, (struct sockaddr *)&a, sizeof(a)) < 0) { perror("bind"); return 1; }

	ChiakiLog log; ChiakiSession s; ChiakiConnectInfo info; int have_session = 0, have_keys = 0; double last_try = -1e9; uint64_t wake_cred = 0;
	chiaki_log_init(&log, CHIAKI_LOG_ALL & ~CHIAKI_LOG_VERBOSE, log_cb, NULL);
	memset(&info, 0, sizeof(info));
	if (!dry) {
		g_logf = fopen(getenv("BRIDGE_LOG") && *getenv("BRIDGE_LOG") ? getenv("BRIDGE_LOG") : "/tmp/gtv-bridge-chiaki.log", "w");
		if (chiaki_lib_init() != CHIAKI_ERR_SUCCESS) { fprintf(stderr, "chiaki init failed\n"); return 1; }
		const char *host = getenv("RP_HOST"), *rk = getenv("RP_REGIST_KEY_HEX"), *mo = getenv("RP_MORNING_HEX"), *ac = getenv("RP_ACCOUNT_HEX");
		if (host && *host && rk && mo && ac && hex2bin(rk, (uint8_t *)info.regist_key, sizeof(info.regist_key)) > 0 && hex2bin(mo, info.morning, 16) == 16 && hex2bin(ac, info.psn_account_id, 8) == 8) {
			have_keys = 1; info.ps5 = true; info.host = host;
			chiaki_connect_video_profile_preset(&info.video_profile, CHIAKI_VIDEO_RESOLUTION_PRESET_720p, CHIAKI_VIDEO_FPS_PRESET_30);
			info.video_profile_auto_downgrade = true; info.enable_dualsense = true; info.audio_video_disabled = CHIAKI_NONE_DISABLED; info.packet_loss_max = 0.05; info.enable_idr_on_fec_failure = true;
			wake_cred = strtoull(info.regist_key, NULL, 16);   // the registration key is 8 hex characters; as a number it is the wake-up credential
		}
	}
	printf("{\"event\":\"started\",\"port\":%d,\"dry_run\":%d,\"paired\":%d}\n", port, dry, have_keys);

	Bike bike; Pad pad, remote; memset(&bike, 0, sizeof(bike)); memset(&pad, 0, sizeof(pad)); memset(&remote, 0, sizeof(remote)); bike.at = pad.at = remote.at = -1e9;
	double last_input = -1e9, last_status = 0; ChiakiControllerState st; chiaki_controller_state_set_idle(&st); long frames_prev = 0; double fps = 0;
	while (!g_stop) {
		fd_set r; FD_ZERO(&r); FD_SET(fd, &r); struct timeval tv = { 0, 8000 };
		double t = now_s();
		if (select(fd + 1, &r, NULL, NULL, &tv) > 0) {
			char buf[512]; ssize_t n = recv(fd, buf, sizeof(buf) - 1, 0);
			if (n > 0) { buf[n] = 0; t = now_s();
				if (!strncmp(buf, "bike", 4)) { bike.power = kv(buf, "power", 0); bike.cadence = kv(buf, "cadence", 0); bike.steer = kv(buf, "steer", 0); bike.brake = (int)kv(buf, "brake", 0); bike.at = t;
					if (bike.cadence > 5 || bike.brake) last_input = t; }   // a trainer that is merely switched on is not activity
				else if (!strncmp(buf, "pad", 3)) { pad.lx = (int)kv(buf, "lx", 0); pad.ly = (int)kv(buf, "ly", 0); pad.rx = (int)kv(buf, "rx", 0); pad.ry = (int)kv(buf, "ry", 0); pad.l2 = (int)kv(buf, "l2", 0); pad.r2 = (int)kv(buf, "r2", 0); pad.buttons = (unsigned)kv(buf, "buttons", 0); pad.at = t;
					if (pad.buttons || pad.l2 > 8 || pad.r2 > 8 || abs(pad.lx) > 6000 || abs(pad.ly) > 6000 || abs(pad.rx) > 6000 || abs(pad.ry) > 6000) last_input = t; }
				else if (!strncmp(buf, "remote", 6)) { remote.lx = (int)kv(buf, "lx", 0); remote.ly = (int)kv(buf, "ly", 0); remote.rx = (int)kv(buf, "rx", 0); remote.ry = (int)kv(buf, "ry", 0); remote.l2 = (int)kv(buf, "l2", 0); remote.r2 = (int)kv(buf, "r2", 0);
					remote.buttons = (unsigned)kv(buf, "buttons", 0); remote.at = t + kv(buf, "ms", 200) / 1000.0; last_input = t; }
				else if (!strncmp(buf, "cfg", 3)) {
					if (has_key(buf, "enabled")) m.enabled = (int)kv(buf, "enabled", 0);
					if (has_key(buf, "full_watts")) m.full_w = kv(buf, "full_watts", m.full_w);
					if (has_key(buf, "min_watts")) m.min_w = kv(buf, "min_watts", m.min_w);
					if (has_key(buf, "sprint_watts")) m.sprint_w = kv(buf, "sprint_watts", m.sprint_w);
					if (has_key(buf, "sprint_hz")) m.sprint_hz = kv(buf, "sprint_hz", m.sprint_hz);
					if (has_key(buf, "steer_full")) m.steer_full = kv(buf, "steer_full", m.steer_full);
					if (has_key(buf, "steer_deadzone")) m.steer_dz = kv(buf, "steer_deadzone", m.steer_dz);
					if (has_key(buf, "idle_s")) m.idle_s = kv(buf, "idle_s", m.idle_s);
					if (has_key(buf, "stop_brake_s")) m.stop_brake_s = kv(buf, "stop_brake_s", m.stop_brake_s); }
				else if (!strncmp(buf, "ctl", 3)) {
					if (strstr(buf, "quit")) g_stop = 1;
					else if (strstr(buf, "poke")) last_input = t;             // "Start ride": connect now without waiting for pedaling
					else if (strstr(buf, "stop")) last_input = -1e9;
					else if (strstr(buf, "wake") && have_keys) { ChiakiErrorCode e = chiaki_discovery_wakeup(&log, NULL, info.host, wake_cred, true); printf("{\"event\":\"wake\",\"ok\":%d}\n", e == CHIAKI_ERR_SUCCESS); } }
			}
		}
		t = now_s(); merge(&bike, &pad, &remote, &m, t, &st);
		int active = m.enabled && (t - last_input < m.idle_s);
		if (!dry && have_keys) {
			if (have_session && g_quit) { chiaki_session_join(&s); chiaki_session_fini(&s); have_session = 0; g_connected = 0; g_quit = 0; last_try = t; }
			if (active && !have_session && t - last_try > 15) {   // the console refuses a reconnect for about 10 s after a session ends
				last_try = t; g_connected = 0; g_quit = 0; g_quit_reason[0] = 0;
				if (!g_video && getenv("BRIDGE_VIDEO_CMD") && *getenv("BRIDGE_VIDEO_CMD")) g_video = popen(getenv("BRIDGE_VIDEO_CMD"), "w");
				if (chiaki_session_init(&s, &info, &log) == CHIAKI_ERR_SUCCESS) { chiaki_session_set_event_cb(&s, event_cb, NULL); chiaki_session_set_video_sample_cb(&s, video_cb, NULL);
					if (chiaki_session_start(&s) == CHIAKI_ERR_SUCCESS) { have_session = 1; printf("{\"event\":\"connecting\"}\n"); } else chiaki_session_fini(&s); }
			}
			if (have_session && !active && !g_quit) { chiaki_session_stop(&s); chiaki_session_join(&s); chiaki_session_fini(&s); have_session = 0; g_connected = 0; g_quit = 0; last_try = t; printf("{\"event\":\"disconnected\",\"reason\":\"%s\"}\n", m.enabled ? "idle" : "ride stopped"); }
			if (have_session && g_connected && !g_quit) chiaki_session_set_controller_state(&s, &st);
			// When the video is being fed to a command, ask for a full picture now and then. Without one a
			// decoder that missed the first frame, or joined a still scene, never produces an image.
			static double last_idr = -1e9;
			if (have_session && g_connected && !g_quit && g_video && t - last_idr > 10) { last_idr = t; chiaki_session_request_idr(&s); }
		}
		if (t - last_status >= 0.5) {
			long f = g_frames; fps = (f - frames_prev) / (t - last_status); frames_prev = f; last_status = t;
			printf("{\"status\":1,\"enabled\":%d,\"active\":%d,\"session\":\"%s\",\"fps\":%.0f,\"r2\":%u,\"l2\":%u,\"lx\":%d,\"ly\":%d,\"buttons\":%u,\"bike_live\":%d,\"pad_live\":%d,\"paired\":%d,\"last_end\":\"%s\"}\n",
				m.enabled, active, !have_session ? "idle" : g_connected ? "connected" : "connecting", fps, st.r2_state, st.l2_state, st.left_x, st.left_y, st.buttons, t - bike.at < 2.0, t - pad.at < 1.0, have_keys, g_quit_reason);
		}
	}
	if (have_session) { chiaki_session_stop(&s); chiaki_session_join(&s); chiaki_session_fini(&s); }
	printf("{\"event\":\"stopped\"}\n"); return 0;
}
