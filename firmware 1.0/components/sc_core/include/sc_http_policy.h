#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* Portable, allocation-free response guards; also exercised by native host tests. */
typedef enum {
    SC_HTTP_OK = 0,
    SC_HTTP_REDIRECT,
    SC_HTTP_AUTH,
    SC_HTTP_STATUS,
    SC_HTTP_CHUNKED,
    SC_HTTP_LENGTH,
    SC_HTTP_HTML,
    SC_HTTP_CONTENT_TYPE,
    SC_HTTP_ENCODING,
    SC_HTTP_IMAGE_PREFIX,
    SC_HTTP_JSON_PREFIX,
} sc_http_result_t;
sc_http_result_t sc_http_check_headers(int status, int64_t length, bool chunked,
    const char *content_type, const char *encoding, int64_t limit, bool image);
sc_http_result_t sc_http_check_prefix(const unsigned char *data, size_t length, bool image);
const char *sc_http_result_name(sc_http_result_t result);
