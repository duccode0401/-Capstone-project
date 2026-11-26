#pragma once

#include <stdbool.h>
#include <stdint.h>
#include <time.h>

typedef struct {
    uint32_t amount_vnd;
    uint32_t sequence;
} sc_signed_order_t;

bool sc_parse_signed_order(const char *json, time_t now, sc_signed_order_t *out);
