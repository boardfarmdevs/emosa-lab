/* SPDX-License-Identifier: Apache-2.0 */
/* The onboarding vectors' agent as a fixture (fixture.h). */
#include "fixture.h"

#include <string.h>

static const char *str(const cJSON *o, const char *key)
{
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(o, key);
    return cJSON_IsString(v) ? v->valuestring : NULL;
}

static double num(const cJSON *o, const char *key)
{
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(o, key);
    return cJSON_IsNumber(v) ? v->valuedouble : -1;
}

static void hexfield(const cJSON *o, const char *key, em_buf *out)
{
    memset(out, 0, sizeof(*out));
    const char *text = str(o, key);
    if (text)
        em_unhex(text, out);
}

/* A fixed-length hex field into out; left zero when absent or of another length. */
static void hexbytes(const cJSON *o, const char *key, uint8_t *out, size_t n)
{
    em_buf b;
    hexfield(o, key, &b);
    if (b.data && b.len == n)
        memcpy(out, b.data, n);
    em_buf_free(&b);
}

void em_test_agent_load(const cJSON *agent, em_test_agent *out)
{
    const cJSON *radio_json = cJSON_GetObjectItemCaseSensitive(agent, "radio");
    memset(out, 0, sizeof(*out));
    em_binding *binding = &out->binding;
    em_parse_mac(str(agent, "al_mac"), binding->local_al);
    em_parse_mac(str(agent, "controller_al"), binding->controller_al);
    memcpy(binding->sources[0], binding->controller_al, 6);
    binding->nsources = 1;
    em_radio_caps *radio = &out->radio;
    em_parse_mac(str(radio_json, "ruid"), radio->ruid);
    radio->max_bss = (uint8_t)num(radio_json, "max_bss");
    radio->advanced_flags = (uint8_t)num(radio_json, "advanced_flags");
    const cJSON *cls;
    cJSON_ArrayForEach(cls, cJSON_GetObjectItemCaseSensitive(radio_json, "operating_classes"))
    {
        em_basic_class *c = &radio->classes[radio->nclasses++];
        c->operating_class = (uint8_t)cJSON_GetArrayItem(cls, 0)->valueint;
        c->max_eirp_dbm = (int8_t)cJSON_GetArrayItem(cls, 1)->valueint;
        const cJSON *ch;
        cJSON_ArrayForEach(ch, cJSON_GetArrayItem(cls, 2))
            c->non_operable[c->nnon_operable++] = (uint8_t)ch->valueint;
    }
    hexbytes(agent, "profile2_ap_capability", radio->profile2, 4);
    const cJSON *dev = cJSON_GetObjectItemCaseSensitive(agent, "m1_device");
    em_m1_device *d = &out->device;
    hexbytes(dev, "uuid", d->uuid, 16);
    em_parse_mac(str(dev, "al_mac"), d->al_mac);
    d->authentication_types = (uint16_t)num(dev, "authentication_types");
    d->encryption_types = (uint16_t)num(dev, "encryption_types");
    d->connection_types = (uint8_t)num(dev, "connection_types");
    d->configuration_methods = (uint16_t)num(dev, "configuration_methods");
    d->wps_state = (uint8_t)num(dev, "wps_state");
    hexfield(dev, "manufacturer", &d->manufacturer);
    hexfield(dev, "model_name", &d->model_name);
    hexfield(dev, "model_number", &d->model_number);
    hexfield(dev, "serial_number", &d->serial_number);
    hexfield(dev, "device_name", &d->device_name);
    hexbytes(dev, "primary_device_type", d->primary_device_type, 8);
    d->rf_band = (uint8_t)num(dev, "rf_band");
    d->association_state = (uint16_t)num(dev, "association_state");
    d->device_password_id = (uint16_t)num(dev, "device_password_id");
    d->configuration_error = (uint16_t)num(dev, "configuration_error");
    d->os_version = (uint32_t)num(dev, "os_version");
    hexfield(agent, "enrollee_private", &out->private_key);
    for (int i = 0; i < 16; i++)
        out->nonce[i] = (uint8_t)i;
    out->entropy = (em_wsc_entropy){out->private_key.data, out->private_key.len, out->nonce};
}

void em_test_agent_free(em_test_agent *a)
{
    em_buf_free(&a->private_key);
    em_buf_free(&a->device.manufacturer);
    em_buf_free(&a->device.model_name);
    em_buf_free(&a->device.model_number);
    em_buf_free(&a->device.serial_number);
    em_buf_free(&a->device.device_name);
}
