/* EMOSA lab glue for the pinned OpenSync OSW dummy-driver API.
 * Synthetic driver feedback only; no OVSDB or network writes from this module.
 * This is a separate C process dependency, never loaded into Python or a pod.
 */
#include <assert.h>
#include <ev.h>
#include <stdlib.h>
#include <unistd.h>
#include <osw_drv_dummy.h>

static void initialize(struct osw_drv *drv);
static void request_config(struct osw_drv *drv, struct osw_drv_conf *conf);
static struct osw_drv_dummy dummy = {
    .name = "emosa-simulated-driver",
    .init_fn = initialize,
    .request_config_fn = request_config,
};
static struct osw_drv_conf *pending;
static ev_timer apply_timer;
static const char *hold_path;
static unsigned sequence;

static void *copy_array(const void *src, size_t count, size_t size)
{
    if (count == 0) return NULL;
    assert(src != NULL && count <= 32);
    void *dst = calloc(count, size);
    assert(dst != NULL);
    memcpy(dst, src, count * size);
    return dst;
}

static void publish_phy(bool enabled, int chainmask, struct osw_reg_domain reg)
{
    struct osw_drv_phy_state phy = {
        .exists = true, .enabled = enabled, .tx_chainmask = chainmask,
        .mac_addr = {.octet = {2, 0, 0, 0, 0x20, 1}}, .reg_domain = reg,
        .n_channel_states = 1,
    };
    phy.channel_states = calloc(1, sizeof(*phy.channel_states));
    assert(phy.channel_states != NULL);
    phy.channel_states[0].channel = (struct osw_channel){
        .width = OSW_CHANNEL_20MHZ, .control_freq_mhz = 5180, .center_freq0_mhz = 5180,
    };
    osw_drv_dummy_set_phy(&dummy, "lab-radio", &phy);
}

static void apply_config(EV_P_ ev_timer *timer, int events)
{
    (void)timer; (void)events;
    if (hold_path != NULL && access(hold_path, F_OK) == 0) {
        ev_timer_set(&apply_timer, 0.1, 0);
        ev_timer_start(EV_A_ &apply_timer);
        return;
    }
    assert(pending != NULL);
    const struct osw_drv_phy_config *phy = &pending->phy_list[0];
    const struct osw_drv_vif_config *config = &phy->vif_list.list[0];
    const struct osw_drv_vif_config_ap *ap = &config->u.ap;
    struct osw_drv_vif_state state = {
        .exists = true, .vif_type = OSW_VIF_AP,
        .status = config->enabled ? OSW_VIF_ENABLED : OSW_VIF_DISABLED,
        .mac_addr = {.octet = {2, 0, 0, 0, 0x10, 1}},
        .tx_power_dbm = config->tx_power_dbm,
    };
#define COPY(field) state.u.ap.field = ap->field
    COPY(isolated); COPY(ssid_hidden); COPY(mcast2ucast); COPY(wps_pbc);
    COPY(beacon_interval_tu); COPY(bridge_if_name); COPY(nas_identifier);
    COPY(mode); COPY(channel); COPY(ssid); COPY(acl_policy); COPY(wpa);
    COPY(multi_ap); COPY(mbss_mode); COPY(mbss_group); COPY(ft_encr_key);
    COPY(ft_over_ds); COPY(ft_pmk_r1_push); COPY(ft_psk_generate_local);
    COPY(ft_pmk_r0_key_lifetime_sec); COPY(ft_pmk_r1_max_key_lifetime_sec);
#undef COPY
#define COPY_LIST(field) do { \
    state.u.ap.field.count = ap->field.count; \
    state.u.ap.field.list = copy_array(ap->field.list, ap->field.count, sizeof(*ap->field.list)); \
} while (0)
    COPY_LIST(psk_list); COPY_LIST(acl); COPY_LIST(neigh_list);
    COPY_LIST(neigh_ft_list); COPY_LIST(wps_cred_list);
#undef COPY_LIST
    publish_phy(phy->enabled, phy->tx_chainmask, phy->reg_domain);
    osw_drv_dummy_set_vif(&dummy, "lab-radio", "lab-ap", &state);
    fprintf(stderr, "EMOSA_NATIVE simulated_feedback sequence=%u ssid_length=%zu psk_count=%zu\n",
            sequence, state.u.ap.ssid.len, state.u.ap.psk_list.count);
    osw_drv_conf_free(pending);
    pending = NULL;
}

static void request_config(struct osw_drv *drv, struct osw_drv_conf *conf)
{
    (void)drv;
    if (conf->n_phy_list != 1 || strcmp(conf->phy_list[0].phy_name, "lab-radio") != 0 ||
        conf->phy_list[0].vif_list.count != 1) goto reject;
    const struct osw_drv_vif_config *vif = &conf->phy_list[0].vif_list.list[0];
    const struct osw_drv_vif_config_ap *ap = &vif->u.ap;
    const struct osw_passpoint empty_passpoint = {0};
    if (strcmp(vif->vif_name, "lab-ap") != 0 || vif->vif_type != OSW_VIF_AP ||
        ap->channel.control_freq_mhz != 5180 || ap->channel.width != OSW_CHANNEL_20MHZ ||
        ap->psk_list.count > 2 || ap->acl.count > 32 || ap->neigh_list.count > 32 ||
        ap->neigh_ft_list.count > 32 || ap->wps_cred_list.count > 32 ||
        ap->radius_list.count != 0 || ap->acct_list.count != 0 ||
        !osw_passpoint_is_equal(&ap->passpoint, &empty_passpoint)) goto reject;
    if (pending != NULL) osw_drv_conf_free(pending);
    pending = conf;
    ++sequence;
    fprintf(stderr, "EMOSA_NATIVE driver_request sequence=%u held=%d ssid_length=%zu psk_count=%zu\n",
            sequence, hold_path != NULL && access(hold_path, F_OK) == 0,
            ap->ssid.len, ap->psk_list.count);
    ev_timer_stop(EV_DEFAULT_ &apply_timer);
    ev_timer_set(&apply_timer, 0.5, 0);
    ev_timer_start(EV_DEFAULT_ &apply_timer);
    return;
reject:
    fprintf(stderr, "EMOSA_NATIVE rejected_request unsupported_synthetic_scope\n");
    osw_drv_conf_free(conf);
}

static void initialize(struct osw_drv *drv)
{
    (void)drv;
    publish_phy(true, 1, (struct osw_reg_domain){.ccode = "US", .iso3166_num = 840,
                                              .dfs = OSW_REG_DFS_FCC});
    struct osw_drv_vif_state state = {
        .exists = true, .status = OSW_VIF_ENABLED, .vif_type = OSW_VIF_AP,
        .mac_addr = {.octet = {2, 0, 0, 0, 0x10, 1}},
        .u.ap = {
            .beacon_interval_tu = 100,
            .channel = {.width = OSW_CHANNEL_20MHZ, .control_freq_mhz = 5180,
                        .center_freq0_mhz = 5180},
            .ssid = {.buf = "initial-network", .len = 15},
            .wpa = {.rsn = true, .akm_psk = true, .pairwise_ccmp = true},
            .psk_list.count = 1,
        },
    };
    state.u.ap.psk_list.list = calloc(1, sizeof(*state.u.ap.psk_list.list));
    assert(state.u.ap.psk_list.list != NULL);
    strcpy(state.u.ap.psk_list.list[0].psk.str, "initial-simulation-key");
    osw_drv_dummy_set_vif(&dummy, "lab-radio", "lab-ap", &state);
    fprintf(stderr, "EMOSA_NATIVE simulated_inventory one_phy_one_existing_ap\n");
}

OSW_MODULE(emosa_native_lab)
{
    char hostname[128] = {0};
    assert(gethostname(hostname, sizeof(hostname)) == 0);
    assert(strcmp(hostname, "opensync-native-r0") == 0);
    assert(getenv("OSW_DRV_TARGET_DISABLED") != NULL);
    assert(getenv("OSW_DRV_NL80211_DISABLE") != NULL);
    hold_path = getenv("EMOSA_NATIVE_HOLD_FILE");
    assert(hold_path != NULL && hold_path[0] == '/');
    ev_timer_init(&apply_timer, apply_config, 0, 0);
    OSW_MODULE_LOAD(osw_drv);
    osw_drv_dummy_init(&dummy);
    return &dummy;
}
