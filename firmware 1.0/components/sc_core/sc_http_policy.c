#include "sc_http_policy.h"

static unsigned lower(unsigned c) { return c >= 'A' && c <= 'Z' ? c + ('a'-'A') : c; }
static bool prefix(const char *value, const char *expected)
{
    while(*expected) if(lower((unsigned char)*value++) != (unsigned char)*expected++) return false;
    return true;
}
static bool media_type(const char *value, const char *expected)
{
    if(!value) return false;
    while(*value==' ' || *value=='\t') value++;
    while(*expected) if(lower((unsigned char)*value++) != (unsigned char)*expected++) return false;
    while(*value==' ' || *value=='\t') value++;
    return *value==0 || *value==';';
}
sc_http_result_t sc_http_check_headers(int status,int64_t length,bool chunked,
    const char *content_type,const char *encoding,int64_t limit,bool image)
{
    if(status>=300 && status<400) return SC_HTTP_REDIRECT;
    if(status==401 || status==403) return SC_HTTP_AUTH;
    if(status!=200) return SC_HTTP_STATUS;
    if(media_type(content_type,"text/html") || media_type(content_type,"application/xhtml+xml")) return SC_HTTP_HTML;
    if(encoding && *encoding && !media_type(encoding,"identity")) return SC_HTTP_ENCODING;
    if(chunked) return SC_HTTP_CHUNKED;
    if(length<=0 || length>limit) return SC_HTTP_LENGTH;
    if(!media_type(content_type,image?"application/octet-stream":"application/json")) return SC_HTTP_CONTENT_TYPE;
    return SC_HTTP_OK;
}
sc_http_result_t sc_http_check_prefix(const unsigned char *data,size_t length,bool image)
{
    if(!data || !length) return image?SC_HTTP_IMAGE_PREFIX:SC_HTTP_JSON_PREFIX;
    size_t i=0;
    while(i<length && (data[i]==' ' || data[i]=='\t' || data[i]=='\r' || data[i]=='\n')) i++;
    /* Prefix helper only reads a NUL-terminated local probe, never beyond data. */
    char probe[16]; size_t n=0;
    while(i+n<length && n<sizeof(probe)-1) { probe[n]=(char)data[i+n]; n++; }
    probe[n]=0;
    if(prefix(probe,"<!doctype html") || prefix(probe,"<html") || prefix(probe,"<head")) return SC_HTTP_HTML;
    if(image) return data[0]==0xe9?SC_HTTP_OK:SC_HTTP_IMAGE_PREFIX;
    return i<length && data[i]=='{'?SC_HTTP_OK:SC_HTTP_JSON_PREFIX;
}
const char *sc_http_result_name(sc_http_result_t result)
{
    switch(result) {
    case SC_HTTP_OK: return "OK";
    case SC_HTTP_REDIRECT: return "HTTPS_REDIRECT_REJECTED";
    case SC_HTTP_AUTH: return "HTTPS_AUTH_OR_CAPABILITY_EXPIRED";
    case SC_HTTP_STATUS: return "HTTPS_STATUS_NOT_200";
    case SC_HTTP_CHUNKED: return "HTTPS_CHUNKED_NOT_SUPPORTED";
    case SC_HTTP_LENGTH: return "HTTPS_CONTENT_LENGTH_INVALID";
    case SC_HTTP_HTML: return "HTTPS_HTML_OR_NGROK_WARNING";
    case SC_HTTP_CONTENT_TYPE: return "HTTPS_CONTENT_TYPE_INVALID";
    case SC_HTTP_ENCODING: return "HTTPS_CONTENT_ENCODING_REJECTED";
    case SC_HTTP_IMAGE_PREFIX: return "ESP_IMAGE_PREFIX_INVALID";
    case SC_HTTP_JSON_PREFIX: return "MANIFEST_JSON_PREFIX_INVALID";
    default: return "HTTPS_RESPONSE_INVALID";
    }
}
