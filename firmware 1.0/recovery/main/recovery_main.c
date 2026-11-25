#include "sc_core.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_ota_ops.h"
#include <string.h>

void app_main(void)
{
    sc_relay_off();
    esp_err_t e=sc_init(NULL,NULL);
    if(e!=ESP_OK) {
        ESP_LOGE("RECOVERY","Provisioning / flash check failed: %s; relay remains OFF",esp_err_to_name(e));
        while(1) vTaskDelay(pdMS_TO_TICKS(1000));
    }
    /* Establish durable factory selection on every recovery entry, even after rollback. */
    ESP_ERROR_CHECK(sc_go_recovery());
    ESP_ERROR_CHECK(sc_recovery_entry());
    sc_network_start(NULL);
    sc_log("WARN","RECOVERING","Recovery running; relay locked OFF");
    unsigned retry_s=5;
    while(1) {
        sc_command_t command;
        if(sc_receive(&command) && command.ota) {
            sc_request_t next;
            if(sc_parse_request(command.data,&next)==ESP_OK && sc_accept_request(&next)==ESP_OK)
                sc_status(&next,"ACCEPTED",0);
            else sc_log("WARN","OTA_REJECTED","Invalid or replayed recovery command");
        }
        sc_request_t request;
        char state[24]={0};
        if(sc_get_state(state,sizeof(state))==ESP_OK && !strcmp(state,"bootstrap")) {
            /* Physical-UART provisioning only; never used for a network download. */
            const esp_partition_t *app=esp_partition_find_first(0,ESP_PARTITION_SUBTYPE_APP_OTA_0,NULL);
            ESP_ERROR_CHECK(sc_set_state("bootstrapped"));
            if(app && esp_ota_set_boot_partition(app)==ESP_OK) esp_restart();
            sc_log("ERROR","BOOTSTRAP_FAILED","Preloaded app invalid; waiting for signed OTA command");
        }
        bool pending=sc_get_state(state,sizeof(state))==ESP_OK &&
            (!strcmp(state,"pending") || !strcmp(state,"candidate"));
        if(pending && sc_load_request(&request)==ESP_OK && sc_download_ready()) {
            sc_status(&request,"RECOVERING",0);
            e=sc_install(&request);
            const char *reason=sc_update_error_name(e);
            bool retry=sc_update_retryable(e);
            /* FAILED is terminal in Django and revokes the download capability.
             * Transient failures retain pending and are announced as RECOVERING.
             * Backend must allow retry transitions / progress restart; see integration guide. */
            if(!retry) ESP_ERROR_CHECK(sc_fail_update(reason));
            sc_status_error(&request,retry?"RECOVERING":"FAILED",0,reason);
            sc_log("ERROR","OTA_FAILED",reason);
            ESP_LOGE("RECOVERY","Install failed: %s; retry=%d",reason,retry);
            sc_memory_log();
            if(retry) {
                /* Keep the job alive during backoff without keeping HTTP/TLS buffers. */
                for(unsigned elapsed=0;elapsed<retry_s;elapsed++) {
                    if(elapsed && elapsed%10==0) sc_status_error(&request,"RECOVERING",0,reason);
                    vTaskDelay(pdMS_TO_TICKS(1000));
                }
                if(retry_s<300) retry_s=retry_s*2>300?300:retry_s*2;
            }
        } else {
            static bool reported;
            if(!strcmp(state,"failed") && sc_network_ready() && !reported && sc_load_request(&request)==ESP_OK) {
                char reason[96]; sc_failure_reason(reason,sizeof(reason));
                sc_status_error(&request,"FAILED",0,reason); reported=true;
                sc_log("ERROR","OTA_FAILED",reason);
            }
            vTaskDelay(pdMS_TO_TICKS(1000));
        }
    }
}
