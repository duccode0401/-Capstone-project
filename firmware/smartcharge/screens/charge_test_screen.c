#include "charge_test_screen.h"

#include <stdio.h>

static lv_obj_t *wifi_label;
static lv_obj_t *charge_label;
static lv_obj_t *voltage_value;
static lv_obj_t *current_value;
static lv_obj_t *power_value;

static lv_obj_t *make_label(lv_obj_t *parent, const char *text, const lv_font_t *font,
                            lv_color_t color, lv_align_t align, int x, int y)
{
    lv_obj_t *label = lv_label_create(parent);
    lv_label_set_text(label, text);
    lv_obj_set_style_text_font(label, font, 0);
    lv_obj_set_style_text_color(label, color, 0);
    lv_obj_align(label, align, x, y);
    return label;
}

static lv_obj_t *make_metric_card(lv_obj_t *parent, int x, const char *title,
                                  const char *initial, const char *unit,
                                  lv_color_t accent, lv_obj_t **value_out)
{
    lv_obj_t *card = lv_obj_create(parent);
    lv_obj_set_size(card, 96, 91);
    lv_obj_set_pos(card, x, 108);
    lv_obj_set_style_bg_color(card, lv_color_hex(0x18283D), 0);
    lv_obj_set_style_bg_opa(card, LV_OPA_COVER, 0);
    lv_obj_set_style_border_width(card, 1, 0);
    lv_obj_set_style_border_color(card, lv_color_hex(0x35516D), 0);
    lv_obj_set_style_radius(card, 10, 0);
    lv_obj_set_style_pad_all(card, 0, 0);
    lv_obj_clear_flag(card, LV_OBJ_FLAG_SCROLLABLE);

    make_label(card, title, &lv_font_montserrat_12, lv_color_hex(0xD6E4F2),
               LV_ALIGN_TOP_MID, 0, 9);
    *value_out = make_label(card, initial, &lv_font_montserrat_20, accent,
                            LV_ALIGN_CENTER, 0, 3);
    make_label(card, unit, &lv_font_montserrat_12, lv_color_hex(0xC0D0E2),
               LV_ALIGN_BOTTOM_MID, 0, -7);
    return card;
}

void charge_test_screen_create(void)
{
    lv_obj_t *root = lv_screen_active();
    lv_obj_set_style_bg_color(root, lv_color_hex(0x070D17), 0);
    lv_obj_set_style_bg_opa(root, LV_OPA_COVER, 0);
    lv_obj_set_style_pad_all(root, 0, 0);
    lv_obj_clear_flag(root, LV_OBJ_FLAG_SCROLLABLE);

    make_label(root, "SMARTCHARGE", &lv_font_montserrat_16,
               lv_color_hex(0xFFFFFF), LV_ALIGN_TOP_LEFT, 14, 9);
    wifi_label = make_label(root, "WIFI OFF", &lv_font_montserrat_12,
                            lv_color_hex(0xFFD166), LV_ALIGN_TOP_RIGHT, -14, 11);

    lv_obj_t *status = lv_obj_create(root);
    lv_obj_set_size(status, 292, 43);
    lv_obj_set_pos(status, 14, 42);
    lv_obj_set_style_bg_color(status, lv_color_hex(0x1B2A3D), 0);
    lv_obj_set_style_bg_opa(status, LV_OPA_COVER, 0);
    lv_obj_set_style_border_width(status, 0, 0);
    lv_obj_set_style_radius(status, 9, 0);
    lv_obj_set_style_pad_all(status, 0, 0);
    lv_obj_clear_flag(status, LV_OBJ_FLAG_SCROLLABLE);

    lv_obj_t *dot = lv_obj_create(status);
    lv_obj_set_size(dot, 10, 10);
    lv_obj_align(dot, LV_ALIGN_LEFT_MID, 14, 0);
    lv_obj_set_style_bg_color(dot, lv_color_hex(0x8492A5), 0);
    lv_obj_set_style_bg_opa(dot, LV_OPA_COVER, 0);
    lv_obj_set_style_border_width(dot, 0, 0);
    lv_obj_set_style_radius(dot, LV_RADIUS_CIRCLE, 0);
    lv_obj_clear_flag(dot, LV_OBJ_FLAG_SCROLLABLE);

    charge_label = lv_label_create(status);
    lv_label_set_text(charge_label, "NO CHARGE");
    lv_obj_set_style_text_font(charge_label, &lv_font_montserrat_16, 0);
    lv_obj_set_style_text_color(charge_label, lv_color_hex(0xF1F5FA), 0);
    lv_obj_align(charge_label, LV_ALIGN_LEFT_MID, 35, 0);

    make_metric_card(root, 14, "VOLTAGE", "--.-", "V", lv_color_hex(0x70D7FF), &voltage_value);
    make_metric_card(root, 112, "CURRENT", "--.--", "A", lv_color_hex(0x72F0B8), &current_value);
    make_metric_card(root, 210, "POWER", "---.-", "W", lv_color_hex(0xFFD166), &power_value);

    make_label(root, "LIVE MONITOR  |  ESP32", &lv_font_montserrat_12,
               lv_color_hex(0xA7B8CC), LV_ALIGN_BOTTOM_MID, 0, -7);
}

void charge_test_screen_set_power(float current, float voltage, float power)
{
    char text[20];
    if (voltage >= 0.0f) {
        snprintf(text, sizeof(text), "%.1f", (double)voltage);
        lv_label_set_text(voltage_value, text);
    }
    if (current >= 0.0f) {
        snprintf(text, sizeof(text), "%.2f", (double)current);
        lv_label_set_text(current_value, text);
    }
    if (power >= 0.0f) {
        snprintf(text, sizeof(text), "%.1f", (double)power);
        lv_label_set_text(power_value, text);
    }
}

void charge_test_screen_set_energy(float energy_Wh, float target_Wh)
{
    (void)energy_Wh;
    (void)target_Wh;
}

void charge_test_screen_set_mode(charge_screen_mode_t mode)
{
    if (mode == CHARGE_SCREEN_CHARGING) charge_test_screen_set_charging(0, 0.0f);
    else if (mode == CHARGE_SCREEN_DONE) charge_test_screen_set_done();
    else charge_test_screen_set_charging(-1, 0.0f);
}

void charge_test_screen_set_charging(int money, float target_Wh)
{
    (void)money;
    (void)target_Wh;
    lv_label_set_text(charge_label, "CHARGING");
    lv_obj_set_style_text_color(charge_label, lv_color_hex(0x63D7A4), 0);
}

void charge_test_screen_set_done(void)
{
    lv_label_set_text(charge_label, "NO CHARGE");
    lv_obj_set_style_text_color(charge_label, lv_color_hex(0xF1F5FA), 0);
}

void charge_test_screen_set_wifi(bool connected)
{
    lv_label_set_text(wifi_label, connected ? "WIFI ON" : "WIFI OFF");
    lv_obj_set_style_text_color(wifi_label,
        connected ? lv_color_hex(0x72F0B8) : lv_color_hex(0xFFD166), 0);
}
