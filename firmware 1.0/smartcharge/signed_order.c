#include "signed_order.h"

#include <inttypes.h>
#include <stdio.h>
#include <string.h>

#include "sc_core.h"

#define ORDER_MAX_AGE_SECONDS 300
#define ORDER_MAX_AMOUNT_VND 10000000

static const char *string_field(const cJSON *json, const char *key)
{
    const cJSON *value=cJSON_GetObjectItemCaseSensitive(json,key);
    return cJSON_IsString(value)?value->valuestring:"";
}

static bool number_field(const cJSON *json,const char *key,uint32_t max,uint32_t *out)
{
    const cJSON *value=cJSON_GetObjectItemCaseSensitive(json,key);
    if(!cJSON_IsNumber(value) || !(value->valuedouble>=1 && value->valuedouble<=max)) return false;
    uint32_t n=(uint32_t)value->valuedouble;
    if(n!=value->valuedouble) return false;
    *out=n;
    return true;
}

bool sc_parse_signed_order(const char *json,time_t now,sc_signed_order_t *out)
{
    if(!sc_command_auth_ready() || now<=1735689600) return false;
    /* cJSON strings are NUL-terminated. Reject escaped fields so their signed
     * interpretation is identical to the wire format, including embedded NUL. */
    if(strchr(json,'\\')) return false;
    cJSON *message=sc_parse_flat_json(json,SC_MESSAGE_MAX-1);
    if(!message) return false;
    const char *device=string_field(message,"device");
    const char *order_id=string_field(message,"order_id");
    size_t id_length=strspn(order_id,"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-");
    uint32_t port,amount,expires,sequence;
    bool valid=!strcmp(device,sc_config.device) &&
        !strcmp(string_field(message,"command"),"START_CHARGE") &&
        id_length>0 && id_length<64 && !order_id[id_length] &&
        number_field(message,"port",1,&port) &&
        number_field(message,"amount_vnd",ORDER_MAX_AMOUNT_VND,&amount) &&
        number_field(message,"expires_at",UINT32_MAX,&expires) &&
        number_field(message,"seq",UINT32_MAX,&sequence) &&
        (int64_t)expires>(int64_t)now && (int64_t)expires-(int64_t)now<=ORDER_MAX_AGE_SECONDS;
    if(valid) {
        char canonical[192];
        int written=snprintf(canonical,sizeof(canonical),"SC-ORDER-1\n%s\nSTART_CHARGE\n%"PRIu32
            "\n%s\n%"PRIu32"\n%"PRIu32"\n%"PRIu32,
            device,port,order_id,amount,expires,sequence);
        valid=written>0 && written<sizeof(canonical) &&
            sc_verify_hmac(canonical,string_field(message,"signature"));
        if(valid) *out=(sc_signed_order_t){.amount_vnd=amount,.sequence=sequence};
    }
    cJSON_Delete(message);
    return valid;
}
